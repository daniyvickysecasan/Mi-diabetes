"""Decisión sobre una sugerencia (hecho / modificado / ignorado) y su resultado a 3–4 h.

Las unidades de "hecho" son las de la sugerencia; las de "modificado" las escribe Daniel. Ninguna
sale de un modelo de lenguaje. La dosis se guarda como una dosis de la app y se concilia con la pluma.

El resultado es solo medición (glucosa a 3 y 4 h, mínimo, máximo, tiempo en rango, hipo): servirá
para ajustar parámetros en la fase 7, siempre como propuesta.
"""
import json
from datetime import datetime, timedelta, timezone

from comun import conciliacion as C

HORAS_RESULTADO = 4
TOLERANCIA_MIN = 10          # lectura más cercana a las 3 y 4 h, como mucho a 10 min


class DecisionNoValida(ValueError):
    pass


def decidir(con, sugerencia_id: int, decision: str, *, unidades: float | None = None, ts: datetime | None = None,
            notas: str | None = None, id_cliente: str | None = None) -> dict:
    """Guarda la decisión y, si hubo inyección, la dosis. Reenviar el mismo id_cliente no duplica nada."""
    ts = ts or datetime.now(timezone.utc)
    if id_cliente:
        previa = con.execute("SELECT * FROM decisiones WHERE id_cliente = %s", (id_cliente,)).fetchone()
        if previa:
            return {"nueva": False, "decision": previa}
    s = con.execute("SELECT * FROM sugerencias WHERE id = %s", (sugerencia_id,)).fetchone()
    if s is None:
        raise DecisionNoValida("No existe esa sugerencia")
    if con.execute("SELECT 1 FROM decisiones WHERE sugerencia_id = %s", (sugerencia_id,)).fetchone():
        raise DecisionNoValida("Esa sugerencia ya tiene una decisión")
    if decision == "hecho":
        if s["bloqueada"]:
            raise DecisionNoValida("Una sugerencia bloqueada no se puede marcar como hecha: usa «modificado»")
        unidades = float(s["u_inmediata"])
    elif decision == "modificado":
        if unidades is None or unidades < 0 or unidades >= 100:
            raise DecisionNoValida("Escribe las unidades que te has puesto")
        if round(unidades * 2) != unidades * 2:
            raise DecisionNoValida("La pluma va de 0,5 en 0,5 U")
    elif decision == "ignorado":
        unidades = None
    else:
        raise DecisionNoValida("decisión: hecho, modificado o ignorado")
    dosis_id = None
    if unidades:
        d = con.execute(
            """INSERT INTO dosis (ts, unidades, insulina, origen, id_externo, comida_id, sugerencia_id, notas)
               VALUES (%s, %s, 'fiasp', 'app', %s, %s, %s, %s) RETURNING id""",
            (ts, unidades, f"decision-{id_cliente or sugerencia_id}", s["comida_id"], sugerencia_id,
             f"sugerencia {sugerencia_id} ({decision})")).fetchone()
        dosis_id = d["id"]
    f = con.execute(
        """INSERT INTO decisiones (sugerencia_id, ts, decision, unidades_reales, dosis_id, notas, id_cliente)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (sugerencia_id, ts, decision, unidades, dosis_id, notas, id_cliente)).fetchone()
    if dosis_id:
        C.conciliar(con)
    return {"nueva": True, "decision": f}


def _cercana(lecturas: list[dict], objetivo: datetime) -> int | None:
    mejor = min(lecturas, key=lambda l: abs(l["ts"] - objetivo), default=None)
    if mejor is None or abs(mejor["ts"] - objetivo) > timedelta(minutes=TOLERANCIA_MIN):
        return None
    return mejor["valor_mgdl"]


def resultado(lecturas: list[dict], inicio: datetime) -> dict:
    """Medición pura de la glucosa en las 4 h siguientes a la sugerencia."""
    tramo = [l for l in lecturas if inicio <= l["ts"] <= inicio + timedelta(hours=HORAS_RESULTADO)]
    valores = [l["valor_mgdl"] for l in tramo]
    if not valores:
        return {"lecturas": 0}
    return {
        "lecturas": len(valores),
        "glucosa_3h": _cercana(tramo, inicio + timedelta(hours=3)),
        "glucosa_4h": _cercana(tramo, inicio + timedelta(hours=4)),
        "minima": min(valores), "maxima": max(valores),
        "pct_en_rango": round(100 * sum(70 <= v <= 180 for v in valores) / len(valores), 1),
        "hipo": any(v < 70 for v in valores),
    }


def evaluar_pendientes(con, ahora: datetime | None = None) -> int:
    """Rellena el resultado de las decisiones con más de 4 h. Devuelve cuántas ha evaluado."""
    ahora = ahora or datetime.now(timezone.utc)
    pendientes = con.execute(
        """SELECT d.id, s.ts FROM decisiones d JOIN sugerencias s ON s.id = d.sugerencia_id
           WHERE d.evaluado_en IS NULL AND s.ts <= %s""", (ahora - timedelta(hours=HORAS_RESULTADO),)).fetchall()
    for p in pendientes:
        lecturas = con.execute(
            """SELECT ts, valor_mgdl FROM glucosa WHERE fuente = 'nightscout' AND tipo = 'sgv'
               AND ts >= %s AND ts <= %s ORDER BY ts""",
            (p["ts"], p["ts"] + timedelta(hours=HORAS_RESULTADO, minutes=TOLERANCIA_MIN))).fetchall()
        con.execute("UPDATE decisiones SET resultado = %s, evaluado_en = %s WHERE id = %s",
                    (json.dumps(resultado(lecturas, p["ts"])), ahora, p["id"]))
    return len(pendientes)


def para_la_app(f: dict | None) -> dict | None:
    if f is None:
        return None
    return {"id": f["id"], "ts": f["ts"].isoformat(), "decision": f["decision"],
            "unidades": float(f["unidades_reales"]) if f["unidades_reales"] is not None else None,
            "dosis_id": f["dosis_id"], "notas": f["notas"], "resultado": f["resultado"]}
