"""Recolector de Nightscout: histórico, ciclo incremental, huecos y verificación.

Idempotente: la glucosa se inserta con ON CONFLICT DO NOTHING sobre (ts, fuente, tipo) y los
tratamientos sobre su _id. Reimportar cualquier periodo no duplica nada.

Los tratamientos escritos por este sistema (comun.origen) se ignoran siempre.
"""
import logging
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from comun.ajustes import Ajustes
from comun.origen import es_origen_propio
from servicios.recoleccion.nightscout import ClienteNightscout

FUENTE = "nightscout"
log = logging.getLogger("recoleccion")


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ normalización
def _ts_ms(ms) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)


def normalizar_entrada(e: dict) -> dict | None:
    """Entrada de Nightscout → fila de `glucosa`, o None si no es una lectura válida."""
    tipo = e.get("type", "sgv")
    clave = {"sgv": "sgv", "mbg": "mbg"}.get(tipo)
    if not clave or e.get("date") is None:
        return None
    try:
        valor = int(round(float(e.get(clave))))
    except (TypeError, ValueError):
        return None
    if not 20 <= valor <= 600:
        return None
    delta = e.get("delta")
    return {
        "ts": _ts_ms(e["date"]),
        "tipo": tipo,
        "valor_mgdl": valor,
        "tendencia": e.get("direction") if tipo == "sgv" else None,
        "delta_mgdl": float(delta) if isinstance(delta, (int, float)) else None,
        "dispositivo": e.get("device"),
        "id_externo": e.get("_id"),
    }


def _ts_tratamiento(t: dict) -> datetime | None:
    if t.get("created_at"):
        return datetime.fromisoformat(str(t["created_at"]).replace("Z", "+00:00"))
    for k in ("mills", "date"):
        if t.get(k) is not None:
            return _ts_ms(t[k])
    return None


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ escritura
def guardar_glucosa(con, entradas: list[dict]) -> dict:
    filas = [f for f in (normalizar_entrada(e) for e in entradas) if f]
    res = {"leidas": len(entradas), "insertadas": 0, "ignoradas": len(entradas) - len(filas), "conflictos": 0}
    if not filas:
        return res
    cols = ["ts", "tipo", "valor_mgdl", "tendencia", "delta_mgdl", "dispositivo", "id_externo"]
    arrays = [[f[c] for f in filas] for c in cols]
    insertadas = con.execute(
        """INSERT INTO glucosa (ts, fuente, tipo, valor_mgdl, tendencia, delta_mgdl, dispositivo, id_externo)
           SELECT u.ts, %s, u.tipo, u.valor, u.tendencia, u.delta, u.dispositivo, u.id_externo
           FROM unnest(%s::timestamptz[], %s::text[], %s::smallint[], %s::text[], %s::real[],
                       %s::text[], %s::text[])
                AS u(ts, tipo, valor, tendencia, delta, dispositivo, id_externo)
           ON CONFLICT (ts, fuente, tipo) DO NOTHING""",
        [FUENTE, *arrays]).rowcount
    res["insertadas"] = insertadas
    # Misma hora y tipo ya guardados con otro valor: se conserva el primero y se cuenta.
    res["conflictos"] = con.execute(
        """SELECT count(*) AS n FROM unnest(%s::timestamptz[], %s::text[], %s::smallint[]) AS u(ts, tipo, valor)
           JOIN glucosa g ON g.ts = u.ts AND g.fuente = %s AND g.tipo = u.tipo AND g.valor_mgdl <> u.valor""",
        [arrays[0], arrays[1], arrays[2], FUENTE]).fetchone()["n"]
    return res


def guardar_tratamientos(con, tratamientos: list[dict]) -> dict:
    res = {"leidos": len(tratamientos), "insertados": 0, "propios_ignorados": 0, "invalidos": 0}
    for t in tratamientos:
        if es_origen_propio(t):
            res["propios_ignorados"] += 1
            continue
        ts = _ts_tratamiento(t)
        if not t.get("_id") or ts is None:
            res["invalidos"] += 1
            continue
        res["insertados"] += con.execute(
            """INSERT INTO tratamientos_nightscout
                   (id_externo, ts, tipo_evento, insulina_u, hidratos_g, introducido_por, datos)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id_externo) DO NOTHING""",
            (t["_id"], ts, t.get("eventType"), _num(t.get("insulin")), _num(t.get("carbs")),
             t.get("enteredBy"), Jsonb(t))).rowcount
    return res


def _registrar(con, modo, desde, hasta, r=None, error=None, inicio=None) -> None:
    r = r or {}
    con.execute(
        """INSERT INTO ejecuciones_recoleccion
               (fuente, modo, inicio, fin, desde, hasta, leidas, insertadas, ignoradas, conflictos, error)
           VALUES (%s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s)""",
        (FUENTE, modo, inicio or ahora_utc(), desde, hasta, r.get("leidas", 0), r.get("insertadas", 0),
         r.get("ignoradas", 0), r.get("conflictos", 0), error))


def _sumar(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


# ------------------------------------------------------------------ huecos
def detectar_huecos(con, desde: datetime, hasta: datetime, minutos: int, ahora: datetime | None = None) -> dict:
    """Recalcula los huecos de glucosa (sgv) entre desde y hasta.

    - Hueco cerrado: dos lecturas consecutivas separadas más de `minutos`.
    - Hueco abierto: la última lectura tiene más de `minutos` de antigüedad.
    - Rellenado: un hueco guardado que ya no existe porque llegaron lecturas tarde.
    """
    ahora = ahora or ahora_utc()
    previa = con.execute(
        "SELECT max(ts) AS ts FROM glucosa WHERE fuente=%s AND tipo='sgv' AND ts < %s", (FUENTE, desde)
    ).fetchone()["ts"]
    limite = timedelta(minutes=minutos)
    pares = con.execute(
        """SELECT anterior, ts FROM (
               SELECT ts, lag(ts) OVER (ORDER BY ts) AS anterior FROM glucosa
               WHERE fuente=%s AND tipo='sgv' AND ts >= %s AND ts <= %s) s
           WHERE ts - anterior > %s""",
        (FUENTE, previa or desde, hasta, limite)).fetchall()
    actuales = {p["anterior"]: p["ts"] for p in pares}
    ultima = con.execute(
        "SELECT max(ts) AS ts FROM glucosa WHERE fuente=%s AND tipo='sgv' AND ts <= %s", (FUENTE, hasta)
    ).fetchone()["ts"]
    if ultima is not None and ahora - ultima > limite and hasta >= ahora - limite:
        actuales[ultima] = None

    for inicio, fin in actuales.items():
        minutos_hueco = int(((fin or ahora) - inicio).total_seconds() // 60)
        con.execute(
            """INSERT INTO huecos (fuente, inicio, fin, minutos, estado) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (fuente, inicio) DO UPDATE
                 SET fin = EXCLUDED.fin, minutos = EXCLUDED.minutos, estado = EXCLUDED.estado,
                     actualizado_en = now()""",
            (FUENTE, inicio, fin, minutos_hueco, "abierto" if fin is None else "cerrado"))
    rellenados = 0
    if ultima is not None:
        rango_desde = previa or desde
        existentes = con.execute(
            """SELECT id, inicio FROM huecos WHERE fuente=%s AND estado <> 'rellenado'
               AND inicio >= %s AND inicio <= %s""", (FUENTE, rango_desde, hasta)).fetchall()
        for h in existentes:
            if h["inicio"] not in actuales:
                con.execute("UPDATE huecos SET estado='rellenado', actualizado_en=now() WHERE id=%s", (h["id"],))
                rellenados += 1
    return {"huecos": len(actuales), "abiertos": sum(1 for v in actuales.values() if v is None),
            "rellenados": rellenados}


# ------------------------------------------------------------------ operaciones
def importar(con, ns: ClienteNightscout, desde: datetime, hasta: datetime, modo: str,
             ventana: timedelta = timedelta(days=7), hueco_min: int = 20) -> dict:
    """Importa glucosa y tratamientos en ventanas. Cada ventana se confirma por separado,
    así que un corte a mitad del histórico se reanuda sin perder lo ya hecho.

    `con` debe estar en modo autocommit (ver comun.bd.conectar(..., autocommit=True))."""
    inicio_ej = ahora_utc()
    total_g, total_t = {}, {}
    t = desde
    try:
        while t < hasta:
            fin = min(t + ventana, hasta)
            with con.transaction():
                total_g = _sumar(total_g, guardar_glucosa(con, ns.entradas(t, fin)))
                total_t = _sumar(total_t, guardar_tratamientos(con, ns.tratamientos(t, fin)))
            t = fin
    except Exception as e:
        _registrar(con, modo, desde, hasta, total_g, error=str(e), inicio=inicio_ej)
        raise
    with con.transaction():
        h = detectar_huecos(con, desde, hasta, hueco_min)
        _registrar(con, modo, desde, hasta, total_g, inicio=inicio_ej)
    return {"glucosa": total_g, "tratamientos": total_t, "huecos": h}


def ciclo_incremental(con, ns: ClienteNightscout, aj: Ajustes, ahora: datetime | None = None) -> dict:
    """Lee desde la última lectura guardada menos el solape (por si llegan datos tarde)."""
    ahora = ahora or ahora_utc()
    ultima = con.execute("SELECT max(ts) AS ts FROM glucosa WHERE fuente=%s", (FUENTE,)).fetchone()["ts"]
    if ultima is None:
        log.warning("La base está vacía: lanza primero el histórico. Importo solo el último día.")
        desde = ahora - timedelta(days=1)
    else:
        desde = min(ultima, ahora) - timedelta(minutes=aj.solape_incremental_min)
    return importar(con, ns, desde, ahora + timedelta(minutes=1), "incremental",
                    ventana=timedelta(days=1), hueco_min=aj.hueco_min)


def verificar(con, ns: ClienteNightscout, desde: datetime, hasta: datetime) -> dict:
    """Compara Nightscout con la base: mismas lecturas y mismos valores (±0 mg/dL)."""
    remotas = {}
    t = desde
    while t < hasta:
        fin = min(t + timedelta(days=7), hasta)
        for e in ns.entradas(t, fin):
            f = normalizar_entrada(e)
            if f and f["tipo"] == "sgv":
                remotas.setdefault(f["ts"], f["valor_mgdl"])
        t = fin
    locales = {f["ts"]: f["valor_mgdl"] for f in con.execute(
        "SELECT ts, valor_mgdl FROM glucosa WHERE fuente=%s AND tipo='sgv' AND ts >= %s AND ts < %s",
        (FUENTE, desde, hasta))}
    faltan = sorted(set(remotas) - set(locales))
    sobran = sorted(set(locales) - set(remotas))
    distintas = sorted(ts for ts in set(remotas) & set(locales) if remotas[ts] != locales[ts])
    return {
        "nightscout": len(remotas), "base": len(locales),
        "faltan_en_base": len(faltan), "sobran_en_base": len(sobran), "valores_distintos": len(distintas),
        "ok": not faltan and not sobran and not distintas,
        "ejemplos_faltan": [x.isoformat() for x in faltan[:5]],
        "ejemplos_distintas": [x.isoformat() for x in distintas[:5]],
    }
