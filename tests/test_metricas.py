"""Métricas de consenso frente a valores de referencia calculados a mano."""
from datetime import timedelta

import pytest

from comun.metricas import calcular, gmi, metricas


@pytest.mark.parametrize("media,esperado", [(100, 5.7), (150, 6.9), (154, 7.0), (200, 8.1), (250, 9.3)])
def test_gmi_tabla_de_bergenstal(media, esperado):
    assert round(gmi(media), 1) == esperado


def test_bandas_con_limites_del_consenso():
    # 10 intervalos: 53 (<54), 54 y 69 (54–69), 70, 100, 180 (rango), 181, 250 (181–250), 251, 300 (>250)
    valores = [53, 54, 69, 70, 100, 180, 181, 250, 251, 300]
    m = calcular(valores, 10)
    assert m["pct_muy_bajo"] == 10.0 and m["pct_bajo"] == 20.0 and m["pct_tbr"] == 30.0
    assert m["pct_en_rango"] == 30.0
    assert m["pct_alto"] == 20.0 and m["pct_muy_alto"] == 20.0 and m["pct_tar"] == 40.0
    assert m["cobertura_pct"] == 100.0


def test_media_de_cv_y_gmi_de_referencia():
    valores = [100, 200] * 50                     # media 150, DE muestral 50,25
    m = calcular(valores, 100)
    assert m["media_mgdl"] == 150.0
    assert m["de_mgdl"] == pytest.approx(50.3, abs=0.05)
    assert m["cv_pct"] == pytest.approx(33.5, abs=0.05)
    assert m["gmi_pct"] == pytest.approx(6.90, abs=0.005)


def test_cobertura_insuficiente():
    m = calcular([120] * 60, 100)
    assert m["cobertura_pct"] == 60.0 and m["suficiente"] is False


def test_sin_datos():
    m = calcular([], 288)
    assert m["pct_en_rango"] is None and m["gmi_pct"] is None and m["cobertura_pct"] == 0.0


def test_metricas_bd_ponderadas_por_tiempo(con, ahora):
    t0 = ahora - timedelta(hours=2)
    for i in range(12):                               # 1 h cada 5 min a 100
        con.execute("INSERT INTO glucosa (ts, valor_mgdl) VALUES (%s, 100)", (t0 + timedelta(minutes=5 * i),))
    for i in range(60):                               # 1 h cada minuto a 300
        con.execute("INSERT INTO glucosa (ts, valor_mgdl) VALUES (%s, 300)", (t0 + timedelta(hours=1, minutes=i),))
    m = metricas(con, t0, ahora)
    assert m["intervalos_5min"] == 24 and m["cobertura_pct"] == 100.0
    assert m["pct_en_rango"] == 50.0 and m["pct_muy_alto"] == 50.0
    assert m["media_mgdl"] == 200.0


def test_coincide_con_conteo_por_lecturas_si_son_cada_5_min(con, ahora):
    """Con lecturas regulares cada 5 min, igual que Nightscout (que cuenta lecturas)."""
    import random
    rnd = random.Random(3)
    t0 = ahora - timedelta(days=1)
    vals = [rnd.randint(45, 320) for _ in range(288)]
    for i, v in enumerate(vals):
        con.execute("INSERT INTO glucosa (ts, valor_mgdl) VALUES (%s, %s)", (t0 + timedelta(minutes=5 * i), v))
    m = metricas(con, t0, ahora)
    tir_ns = 100 * sum(70 <= v <= 180 for v in vals) / len(vals)
    assert abs(m["pct_en_rango"] - tir_ns) < 0.1
    assert abs(m["gmi_pct"] - gmi(sum(vals) / len(vals))) < 0.01
