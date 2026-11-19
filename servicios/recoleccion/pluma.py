"""Dosis de la pluma inteligente leídas de LibreView: normalización, guardado y envío a Nightscout.

Flujo de un ciclo (cada INTERVALO_PLUMA_S):
  1. Pide a la fuente (LibreView o un simulador) las dosis de las últimas SOLAPE_PLUMA_H horas.
  2. Guarda cada una en `dosis` (origen 'pluma_libreview', Fiasp) con ON CONFLICT DO NOTHING:
     releer el mismo periodo no duplica nada. Anota en `llegadas_pluma` cuándo la vio.
  3. Llama a la conciliación (comun.conciliacion), que empareja con lo confirmado en la app.
  4. Envía a Nightscout las dosis pendientes, como tratamientos con enteredBy de origen propio,
     que el recolector de Nightscout ignora al leer (sin bucle de duplicados).

Reglas: la pluma manda en hora y unidades; aquí no se calcula ni se corrige ninguna dosis,
solo se copia lo que registró la pluma. Todo lo de LibreView es Fiasp.
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from psycopg.types.json import Jsonb

from comun.origen import ORIGEN_PROPIO

FUENTE = "libreview"
ORIGEN_DOSIS = "pluma_libreview"
ENTERED_BY = f"{ORIGEN_PROPIO}/libreview"
log = logging.getLogger("pluma")


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DosisPluma:
    """Una inyección tal como la registró la pluma. `ts` siempre con zona (UTC al guardar)."""
    ts: datetime
    unidades: Decimal
    serie_pluma: str | None = None
    id_libreview: str | None = None
    cebado: bool = False
    bruto: dict = field(default_factory=dict, compare=False)

    @property
    def id_externo(self) -> str:
        """Id estable: el de LibreView si lo da; si no, hash de serie + hora + unidades."""
        if self.id_libreview:
            return f"lv:{self.id_libreview}"
        clave = f"{self.serie_pluma or ''}|{self.ts.astimezone(timezone.utc).isoformat()}|{self.unidades}"
        return "lvh:" + hashlib.sha256(clave.encode()).hexdigest()[:16]


class FuenteDosis(Protocol):
    def dosis(self, desde: datetime, hasta: datetime) -> list[DosisPluma]: ...


def unidades_validas(valor) -> Decimal | None:
    """Unidades como Decimal con 2 decimales, o None si no es una dosis guardable."""
    try:
        u = Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None
    return u if Decimal("0") < u < Decimal("1000") else None


# ------------------------------------------------------------------ guardado
def guardar_dosis(con, dosis: list[DosisPluma], modo: str, ahora: datetime | None = None) -> dict:
    """Inserta las dosis nuevas. Devuelve recuentos. Idempotente por (origen, id_externo)."""
    ahora = ahora or ahora_utc()
    res = {"leidas": len(dosis), "insertadas": 0, "ya_estaban": 0, "invalidas": 0, "futuras": 0, "ids": []}
    for d in dosis:
        u = unidades_validas(d.unidades)
        if u is None or d.ts.tzinfo is None:
            res["invalidas"] += 1
            continue
        if d.ts > ahora + timedelta(minutes=10):     # reloj de la pluma adelantado: se guarda y se avisa
            res["futuras"] += 1
            log.warning("Dosis con hora futura (%s): revisa el reloj de la pluma o del iPhone", d.ts)
        notas = "cebado" if d.cebado else None
        fila = con.execute(
            """INSERT INTO dosis (ts, unidades, insulina, origen, id_externo, notas, numero_serie)
               VALUES (%s, %s, 'fiasp', %s, %s, %s, %s)
               ON CONFLICT (origen, id_externo) DO NOTHING RETURNING id""",
            (d.ts.astimezone(timezone.utc), u, ORIGEN_DOSIS, d.id_externo, notas, d.serie_pluma)).fetchone()
        if fila is None:
            res["ya_estaban"] += 1
            continue
        con.execute(
            """INSERT INTO llegadas_pluma (dosis_id, vista_en, modo, serie_pluma, cebado, bruto)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (fila["id"], ahora, modo, d.serie_pluma, d.cebado, Jsonb(d.bruto)))
        # Los cebados no van a Nightscout: no son insulina que llegue al cuerpo.
        if not d.cebado:
            con.execute("INSERT INTO envios_nightscout (dosis_id) VALUES (%s) ON CONFLICT DO NOTHING",
                        (fila["id"],))
        res["insertadas"] += 1
        res["ids"].append(fila["id"])
    return res


def guardar_horas_ambiguas(con, horas: list[dict], serie_pluma: str | None = None) -> int:
    """Horas con varias inyecciones: LibreView solo da la suma de la hora, así que no se inventa el
    reparto. Se guardan aparte (horas_pluma_libreview) para que las cubran la app o el CSV."""
    for h in horas:
        con.execute(
            """INSERT INTO horas_pluma_libreview (hora, inyecciones, unidades_total, serie_pluma)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (hora) DO UPDATE SET inyecciones = EXCLUDED.inyecciones,
                   unidades_total = EXCLUDED.unidades_total, serie_pluma = EXCLUDED.serie_pluma,
                   actualizada_en = now()""",
            (h["hora"], h["inyecciones"], h["unidades_total"], serie_pluma))
    return len(horas)


def conciliar(con) -> dict | None:
    """Llama a la conciliación del hilo de dosis si ya existe (comun.conciliacion)."""
    try:
        from comun.conciliacion import conciliar as _conciliar
    except ImportError:
        return None
    return _conciliar(con)


# ------------------------------------------------------------------ Nightscout
def tratamiento_nightscout(dosis_id: int, ts: datetime, unidades: Decimal, id_externo: str) -> dict:
    """Dosis → tratamiento de Nightscout. enteredBy con origen propio: el recolector lo ignora."""
    return {
        "eventType": "Correction Bolus",
        "insulin": float(unidades),
        "created_at": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "enteredBy": ENTERED_BY,
        "notes": f"Fiasp (pluma, LibreView) {id_externo}",
        "insulinType": "Fiasp",
        "origen_mi_diabetes": True,
        "mi_diabetes_dosis_id": dosis_id,
    }


def enviar_pendientes(con, escritor, limite: int = 50) -> dict:
    """Envía a Nightscout las dosis de la pluma aún no enviadas. Un fallo deja la fila pendiente."""
    res = {"enviadas": 0, "errores": 0}
    if escritor is None:
        return res | {"desactivado": True}
    filas = con.execute(
        """SELECT e.dosis_id, d.ts, d.unidades, d.id_externo FROM envios_nightscout e
           JOIN dosis d ON d.id = e.dosis_id
           WHERE e.estado <> 'enviado' AND d.estado_conciliacion <> 'descartada'
           ORDER BY d.ts LIMIT %s""", (limite,)).fetchall()
    for f in filas:
        t = tratamiento_nightscout(f["dosis_id"], f["ts"], f["unidades"], f["id_externo"])
        try:
            ns_id = escritor.crear_tratamiento(t)
        except Exception as e:
            con.execute("""UPDATE envios_nightscout SET estado='error', intentos=intentos+1, error=%s
                           WHERE dosis_id=%s""", (str(e)[:500], f["dosis_id"]))
            res["errores"] += 1
            continue
        con.execute("""UPDATE envios_nightscout SET estado='enviado', intentos=intentos+1, error=NULL,
                           nightscout_id=%s, enviado_en=now() WHERE dosis_id=%s""", (ns_id, f["dosis_id"]))
        res["enviadas"] += 1
    return res


# ------------------------------------------------------------------ ejecuciones
def _registrar(con, modo, desde, hasta, inicio, r=None, error=None) -> None:
    r = r or {}
    con.execute(
        """INSERT INTO ejecuciones_recoleccion
               (fuente, modo, inicio, fin, desde, hasta, leidas, insertadas, ignoradas, error)
           VALUES (%s, %s, %s, now(), %s, %s, %s, %s, %s, %s)""",
        (FUENTE, modo, inicio, desde, hasta, r.get("leidas", 0), r.get("insertadas", 0),
         r.get("invalidas", 0), error))


def importar(con, fuente: FuenteDosis, desde: datetime, hasta: datetime, modo: str,
             escritor=None, ahora: datetime | None = None) -> dict:
    """Lee, guarda, concilia y envía. `con` en autocommit (comun.bd.conectar(..., autocommit=True))."""
    inicio = ahora_utc()
    try:
        leidas = fuente.dosis(desde, hasta)
        horas = getattr(fuente, "horas_ambiguas", list)()   # solo la fuente del informe las tiene
        with con.transaction():
            r = guardar_dosis(con, leidas, modo, ahora=ahora)
            r["horas_ambiguas"] = guardar_horas_ambiguas(con, horas, getattr(fuente, "serie_pluma", None))
            c = conciliar(con)
            _registrar(con, modo, desde, hasta, inicio, r)
    except Exception as e:
        _registrar(con, modo, desde, hasta, inicio, error=str(e)[:500])
        raise
    envio = enviar_pendientes(con, escritor)
    r.pop("ids", None)
    return {"dosis": r, "conciliacion": c, "nightscout": envio}


def ciclo(con, fuente: FuenteDosis, solape_h: int, escritor=None, ahora: datetime | None = None) -> dict:
    ahora = ahora or ahora_utc()
    return importar(con, fuente, ahora - timedelta(hours=solape_h), ahora + timedelta(minutes=5),
                    "incremental", escritor=escritor, ahora=ahora)


# ------------------------------------------------------------------ retraso
def informe_retraso(con, dias: int = 7, ahora: datetime | None = None) -> dict:
    """Retraso inyección → visto (solo ciclo normal) y comparación con la app cuando hay pareja."""
    ahora = ahora or ahora_utc()
    r = con.execute(
        """SELECT count(*) AS n,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY retraso_pluma_min) AS mediana_min,
                  percentile_cont(0.9) WITHIN GROUP (ORDER BY retraso_pluma_min) AS p90_min,
                  max(retraso_pluma_min) AS max_min,
                  count(dosis_app_id) AS con_app,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY retraso_app_min) AS mediana_app_min,
                  count(*) FILTER (WHERE dosis_app_id IS NOT NULL AND retraso_app_min < retraso_pluma_min)
                      AS app_antes
           FROM retraso_pluma WHERE modo = 'incremental' AND ts_inyeccion >= %s""",
        (ahora - timedelta(days=dias),)).fetchone()
    return {k: (round(float(v), 1) if isinstance(v, (float, Decimal)) else v) for k, v in r.items()} | {"dias": dias}
