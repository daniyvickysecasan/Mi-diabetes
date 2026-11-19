"""Métricas de consenso de la monitorización continua (Battelino et al., Diabetes Care 2019).

- TBR nivel 2: < 54 mg/dL · TBR nivel 1: 54–69 · TIR: 70–180 (ambos incluidos)
- TAR nivel 1: 181–250 · TAR nivel 2: > 250
- GMI (%) = 3,31 + 0,02392 × media en mg/dL (Bergenstal et al., Diabetes Care 2018)
- CV (%) = desviación típica / media × 100 (objetivo ≤ 36 %)
- Cobertura: % del tiempo con datos; el consenso pide ≥ 70 % en 14 días.

Igual que `comun.glucosa.tir`, se pondera por tiempo: las lecturas se agrupan en intervalos de
5 min (media por intervalo) para que un tramo con lecturas cada minuto no pese más.

    python -m comun.metricas --desde 2026-11-01 --hasta 2026-11-15   (fechas en Europe/Madrid)
"""
import argparse
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

INTERVALO_S = 300


def gmi(media_mgdl: float) -> float:
    return 3.31 + 0.02392 * media_mgdl


def calcular(valores: list[float], intervalos_esperados: int) -> dict:
    """Métricas a partir de las medias por intervalo de 5 min (función pura)."""
    n = len(valores)
    cobertura = round(100 * n / max(1, intervalos_esperados), 1)
    if n == 0:
        return {"intervalos_5min": 0, "cobertura_pct": cobertura, "suficiente": False, "media_mgdl": None,
                "de_mgdl": None, "cv_pct": None, "gmi_pct": None, "pct_muy_bajo": None, "pct_bajo": None,
                "pct_tbr": None, "pct_en_rango": None, "pct_alto": None, "pct_muy_alto": None, "pct_tar": None}
    cuenta = {"muy_bajo": 0, "bajo": 0, "rango": 0, "alto": 0, "muy_alto": 0}
    for v in valores:
        if v < 54:
            cuenta["muy_bajo"] += 1
        elif v < 70:
            cuenta["bajo"] += 1
        elif v <= 180:
            cuenta["rango"] += 1
        elif v <= 250:
            cuenta["alto"] += 1
        else:
            cuenta["muy_alto"] += 1
    pct = {k: 100 * c / n for k, c in cuenta.items()}
    media = sum(valores) / n
    de = math.sqrt(sum((v - media) ** 2 for v in valores) / (n - 1)) if n > 1 else 0.0
    r1 = lambda x: round(x, 1)
    return {
        "intervalos_5min": n,
        "cobertura_pct": cobertura,
        "suficiente": cobertura >= 70,
        "media_mgdl": r1(media),
        "de_mgdl": r1(de),
        "cv_pct": r1(100 * de / media),
        "gmi_pct": round(gmi(media), 2),
        "pct_muy_bajo": r1(pct["muy_bajo"]),
        "pct_bajo": r1(pct["bajo"]),
        "pct_tbr": r1(pct["muy_bajo"] + pct["bajo"]),
        "pct_en_rango": r1(pct["rango"]),
        "pct_alto": r1(pct["alto"]),
        "pct_muy_alto": r1(pct["muy_alto"]),
        "pct_tar": r1(pct["alto"] + pct["muy_alto"]),
    }


def metricas(con, desde: datetime, hasta: datetime, fuente: str = "nightscout") -> dict:
    filas = con.execute(
        """SELECT avg(valor_mgdl)::float8 AS v FROM glucosa
           WHERE fuente = %s AND tipo = 'sgv' AND ts >= %s AND ts < %s
           GROUP BY time_bucket('5 minutes', ts)""", (fuente, desde, hasta)).fetchall()
    esperados = int((hasta - desde).total_seconds() // INTERVALO_S)
    res = calcular([f["v"] for f in filas], esperados)
    res["desde"], res["hasta"] = desde.isoformat(), hasta.isoformat()
    res["dias"] = round((hasta - desde).total_seconds() / 86400, 1)
    return res


def _main():
    from comun.ajustes import Ajustes
    from comun.bd import conectar
    p = argparse.ArgumentParser(description="Métricas de consenso de un periodo (fechas locales).")
    p.add_argument("--desde", required=True, help="AAAA-MM-DD, inicio del día en Europe/Madrid")
    p.add_argument("--hasta", required=True, help="AAAA-MM-DD, incluido (hasta el final de ese día)")
    a = p.parse_args()
    aj = Ajustes.desde_entorno()
    zona = ZoneInfo(aj.zona_horaria)
    d = datetime.fromisoformat(a.desde).replace(tzinfo=zona).astimezone(timezone.utc)
    h = (datetime.fromisoformat(a.hasta) + timedelta(days=1)).replace(tzinfo=zona).astimezone(timezone.utc)
    with conectar(aj.database_url) as con:
        m = metricas(con, d, h)
    nombres = [("pct_muy_bajo", "< 54"), ("pct_bajo", "54–69"), ("pct_en_rango", "70–180 (TIR)"),
               ("pct_alto", "181–250"), ("pct_muy_alto", "> 250")]
    print(f"Periodo {a.desde} a {a.hasta} ({m['dias']} días), datos {m['cobertura_pct']} % del tiempo")
    for k, n in nombres:
        print(f"  {n:>14}: {m[k]} %")
    print(f"  TBR {m['pct_tbr']} % · TAR {m['pct_tar']} %")
    print(f"  Media {m['media_mgdl']} mg/dL · DE {m['de_mgdl']} · CV {m['cv_pct']} % · GMI {m['gmi_pct']} %")


if __name__ == "__main__":
    _main()
