"""Consultas de glucosa y métricas sencillas (TIR). Funciones puras sobre la base.

El TIR se calcula por tiempo y no por número de lecturas: las lecturas se agrupan en
intervalos de 5 min (media por intervalo) para que un periodo con lecturas cada minuto no pese
más que uno con lecturas cada 15 min.
"""
from datetime import datetime, timedelta, timezone

FLECHAS = {
    "DoubleUp": "⇈", "SingleUp": "↑", "FortyFiveUp": "↗", "Flat": "→",
    "FortyFiveDown": "↘", "SingleDown": "↓", "DoubleDown": "⇊",
    "NOT COMPUTABLE": "?", "RATE OUT OF RANGE": "?", "NONE": "",
}


def tendencia_por_pendiente(mgdl_por_min: float | None) -> str | None:
    """Misma convención que Nightscout/Dexcom (mg/dL por minuto)."""
    if mgdl_por_min is None:
        return None
    p = mgdl_por_min
    if p > 3: return "DoubleUp"
    if p > 2: return "SingleUp"
    if p > 1: return "FortyFiveUp"
    if p >= -1: return "Flat"
    if p >= -2: return "FortyFiveDown"
    if p >= -3: return "SingleDown"
    return "DoubleDown"


def actual(con, fuente: str = "nightscout", ahora: datetime | None = None) -> dict | None:
    """Última lectura, su antigüedad, delta respecto a ~5 min antes y tendencia."""
    ahora = ahora or datetime.now(timezone.utc)
    filas = con.execute(
        """SELECT ts, valor_mgdl, tendencia FROM glucosa
           WHERE fuente = %s AND tipo = 'sgv' AND ts <= %s
           ORDER BY ts DESC LIMIT 30""", (fuente, ahora)).fetchall()
    if not filas:
        return None
    ultima = filas[0]
    previa = next((f for f in filas[1:] if (ultima["ts"] - f["ts"]) >= timedelta(minutes=4)), None)
    delta = pendiente = None
    if previa and (ultima["ts"] - previa["ts"]) <= timedelta(minutes=16):
        minutos = (ultima["ts"] - previa["ts"]).total_seconds() / 60
        pendiente = (ultima["valor_mgdl"] - previa["valor_mgdl"]) / minutos
        delta = round(pendiente * 5, 1)          # normalizado a 5 min
    tendencia = ultima["tendencia"] or tendencia_por_pendiente(pendiente)
    edad = (ahora - ultima["ts"]).total_seconds() / 60
    return {
        "ts": ultima["ts"].isoformat(),
        "valor_mgdl": ultima["valor_mgdl"],
        "tendencia": tendencia,
        "flecha": FLECHAS.get(tendencia or "", ""),
        "delta_5min_mgdl": delta,
        "minutos_desde_lectura": round(edad, 1),
        "antigua": edad > 15,
    }


def serie(con, desde: datetime, hasta: datetime, fuente: str = "nightscout") -> list[dict]:
    filas = con.execute(
        """SELECT ts, valor_mgdl FROM glucosa
           WHERE fuente = %s AND tipo = 'sgv' AND ts >= %s AND ts <= %s ORDER BY ts""",
        (fuente, desde, hasta)).fetchall()
    return [{"ts": f["ts"].isoformat(), "valor_mgdl": f["valor_mgdl"]} for f in filas]


def tir(con, desde: datetime, hasta: datetime, bajo: int = 70, alto: int = 180,
        fuente: str = "nightscout") -> dict:
    """Porcentaje de tiempo bajo, en rango (bajo–alto, ambos incluidos) y alto.

    `cobertura` es el % de intervalos de 5 min con datos; el consenso pide ≥ 70 %.
    """
    f = con.execute(
        """WITH b AS (
               SELECT time_bucket('5 minutes', ts) AS t, avg(valor_mgdl) AS v
               FROM glucosa WHERE fuente = %(f)s AND tipo = 'sgv' AND ts >= %(d)s AND ts < %(h)s
               GROUP BY 1)
           SELECT count(*) AS n,
                  count(*) FILTER (WHERE v < 54)              AS muy_bajo,
                  count(*) FILTER (WHERE v < %(b)s)           AS bajo,
                  count(*) FILTER (WHERE v >= %(b)s AND v <= %(a)s) AS rango,
                  count(*) FILTER (WHERE v > %(a)s)           AS alto,
                  count(*) FILTER (WHERE v > 250)             AS muy_alto,
                  avg(v) AS media
           FROM b""", {"f": fuente, "d": desde, "h": hasta, "b": bajo, "a": alto}).fetchone()
    esperados = max(1, int((hasta - desde).total_seconds() // 300))
    n = f["n"]
    pct = (lambda k: round(100 * f[k] / n, 1) if n else None)
    return {
        "desde": desde.isoformat(), "hasta": hasta.isoformat(),
        "rango_mgdl": [bajo, alto],
        "intervalos_5min": n,
        "cobertura_pct": round(100 * n / esperados, 1),
        "pct_muy_bajo": pct("muy_bajo"), "pct_bajo": pct("bajo"), "pct_en_rango": pct("rango"),
        "pct_alto": pct("alto"), "pct_muy_alto": pct("muy_alto"),
        "media_mgdl": round(float(f["media"]), 1) if f["media"] is not None else None,
    }
