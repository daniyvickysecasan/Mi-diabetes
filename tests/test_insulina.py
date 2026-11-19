"""Insulina activa de Fiasp frente a una curva de referencia independiente.

La referencia es la integral numérica de la ACTIVIDAD de oref0 (otra fórmula, no la cerrada de la
insulina activa): IOB(t) = U − ∫₀ᵗ actividad. El criterio del plan es error < 0,05 U.
"""
from datetime import datetime, timedelta, timezone

import pytest

from comun.insulina import FIASP, Curva, insulina_activa, insulina_activa_bd, proyeccion

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def referencia(unidades: float, minutos: float, curva: Curva = FIASP, pasos: int = 20000) -> float:
    if minutos <= 0:
        return unidades
    m = min(minutos, curva.duracion_min)
    h = m / pasos
    integral = sum(curva.actividad((i + 0.5) * h) for i in range(pasos)) * h
    return unidades * (1 - integral)


@pytest.mark.parametrize("unidades", [0.5, 1, 4, 8.5, 15])
@pytest.mark.parametrize("minutos", [0, 5, 15, 30, 55, 60, 90, 120, 180, 240, 290, 300, 360])
def test_iob_frente_a_referencia(unidades, minutos):
    r = insulina_activa([{"ts": T0, "unidades": unidades}], T0 + timedelta(minutes=minutos))
    assert abs(r["unidades"] - referencia(unidades, minutos)) < 0.05


def test_valores_de_la_curva_fiasp():
    """Valores fijos para detectar cualquier cambio accidental de la curva (pico 55 min, 5 h)."""
    esperado = {0: 1.0, 30: 0.8843, 60: 0.6679, 120: 0.2883, 180: 0.0884, 240: 0.0146, 300: 0.0}
    for t, v in esperado.items():
        assert FIASP.fraccion_activa(t) == pytest.approx(v, abs=1e-4)


def test_actividad_maxima_en_el_pico_y_area_uno():
    pico = max(range(1, 300), key=FIASP.actividad)
    assert pico == 55
    h = 0.01
    area = sum(FIASP.actividad((i + 0.5) * h) for i in range(int(300 / h))) * h
    assert area == pytest.approx(1.0, abs=1e-6)


def test_monotona_decreciente():
    valores = [FIASP.fraccion_activa(t) for t in range(0, 301)]
    assert all(a >= b for a, b in zip(valores, valores[1:]))


def test_suma_de_dosis_y_futuras_no_cuentan():
    dosis = [{"ts": T0, "unidades": 4}, {"ts": T0 + timedelta(minutes=60), "unidades": 2},
             {"ts": T0 + timedelta(minutes=200), "unidades": 6}]       # aún no puesta
    ahora = T0 + timedelta(minutes=120)
    r = insulina_activa(dosis, ahora)
    esperado = referencia(4, 120) + referencia(2, 60)
    assert abs(r["unidades"] - esperado) < 0.05
    assert len(r["dosis"]) == 2


def test_curva_invalida():
    with pytest.raises(ValueError):
        Curva(pico_min=200, duracion_min=300)


def test_proyeccion_baja_a_cero():
    p = proyeccion([{"ts": T0, "unidades": 5}], T0, horas=5)
    assert p[0]["unidades"] == 5 and p[-1]["unidades"] == 0
    assert all(a["unidades"] >= b["unidades"] for a, b in zip(p, p[1:]))


def _dosis(con, ts, u, origen="app", estado="pendiente", par=None, insulina="fiasp", id_ext=None):
    return con.execute(
        """INSERT INTO dosis (ts, unidades, insulina, origen, estado_conciliacion, dosis_par_id, id_externo)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (ts, u, insulina, origen, estado, par, id_ext)).fetchone()["id"]


def test_iob_bd_no_cuenta_dos_veces_una_pareja(con):
    """App + pluma emparejadas = UNA inyección, con las unidades de la pluma."""
    a = _dosis(con, T0, 4, "app", id_ext="c1")
    p = _dosis(con, T0 + timedelta(minutes=3), 4.5, "pluma_libreview", id_ext="lv1")
    con.execute("UPDATE dosis SET estado_conciliacion='discrepancia', dosis_par_id=%s WHERE id=%s", (p, a))
    con.execute("UPDATE dosis SET estado_conciliacion='discrepancia', dosis_par_id=%s WHERE id=%s", (a, p))
    _dosis(con, T0, 34, "app", insulina="tresiba", id_ext="c2")          # la Tresiba no cuenta
    ahora = T0 + timedelta(minutes=63)
    r = insulina_activa_bd(con, ahora)
    assert abs(r["unidades"] - referencia(4.5, 60)) < 0.05
    assert [d["id"] for d in r["dosis"]] == [p]
