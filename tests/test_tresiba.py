"""Tresiba: configuración con historial y dosis diarias que se dan por puestas."""
from datetime import datetime, time, timedelta, timezone

from comun import tresiba as T

U = lambda *a: datetime(*a, tzinfo=timezone.utc)


def cfg(desde, u, h, m=0, id=1):
    return {"id": id, "vigente_desde": desde, "unidades": u, "hora_local": time(h, m), "zona_horaria": "Europe/Madrid"}


def test_programadas_con_cambio_de_dosis_y_de_hora():
    configs = [cfg(U(2026, 11, 1), 34, 8), cfg(U(2026, 11, 3, 12), 32, 7, 30, id=2)]
    p = T.programadas(configs, U(2026, 11, 1), U(2026, 11, 5))
    assert [(x["ts"], x["unidades"]) for x in p] == [
        (U(2026, 11, 1, 7), 34), (U(2026, 11, 2, 7), 34), (U(2026, 11, 3, 7), 34),
        (U(2026, 11, 4, 6, 30), 32)]


def test_programadas_respetan_el_horario_de_verano():
    p = T.programadas([cfg(U(2026, 3, 1), 34, 8)], U(2026, 3, 28), U(2026, 3, 30))
    assert [x["ts"] for x in p] == [U(2026, 3, 28, 7), U(2026, 3, 29, 6)]


def test_sin_configuracion():
    assert T.programadas([], U(2026, 1, 1), U(2026, 1, 2)) == []


def test_bd_vigente_e_historial(con):
    T.guardar(con, 34, time(8, 0), U(2026, 11, 1))
    T.guardar(con, 36, time(8, 30), U(2026, 11, 10), notas="subo 2")
    assert T.vigente(con, U(2026, 11, 5))["unidades"] == 34
    v = T.vigente(con, U(2026, 11, 12))
    assert v["unidades"] == 36 and v["hora_local"] == "08:30"
    assert [h["unidades"] for h in T.historial(con)] == [36, 34]
    assert T.vigente(con, U(2026, 10, 1)) is None
    assert len(T.programadas_bd(con, U(2026, 11, 1), U(2026, 11, 15))) == 14
