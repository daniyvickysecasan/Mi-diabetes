"""Sugerencias con la base real: parámetros aprobados, lectura de la pluma, API. AFECTA A LA DOSIS."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from api.main import crear_app
from comun import parametros as P
from comun import sugerir as SG
from tests.autenticador_software import Autenticador
from tests.test_api import ORIGEN, _alta


def _glucosa(con, ahora, valor=150, pendiente=0.0, hace_min=2):
    for i, m in enumerate((hace_min, hace_min + 5)):
        con.execute("""INSERT INTO glucosa (ts, valor_mgdl, tendencia) VALUES (%s, %s, NULL)""",
                    (ahora - timedelta(minutes=m), round(valor - pendiente * 5 * i)))


def _parametros(con, aprobar=True, ratio=None, sens=None, confianza="media"):
    v = P.proponer(con, ratio or [{"desde": "00:00", "g_por_u": 10}, {"desde": "20:00", "g_por_u": 12}],
                   sens or [{"desde": "00:00", "mgdl_por_u": 40}], "prueba", confianza=confianza)
    return P.aprobar(con, v["id"], "Daniel") if aprobar else v


def _lectura_ok(con):
    s = con.execute("INSERT INTO solicitudes_lectura_pluma DEFAULT VALUES RETURNING id").fetchone()
    con.execute("UPDATE solicitudes_lectura_pluma SET atendida_en = now(), resultado = 'ok' WHERE id = %s", (s["id"],))
    return s["id"]


@pytest.fixture
def ahora():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ parámetros
def test_proponer_no_activa_y_aprobar_retira_la_anterior(con):
    v1 = _parametros(con, aprobar=False)
    assert v1["estado"] == "propuesta" and P.activa(con) is None
    P.aprobar(con, v1["id"], "Daniel")
    v2 = _parametros(con, aprobar=False)
    P.aprobar(con, v2["id"], "Daniel")
    estados = {f["id"]: f["estado"] for f in con.execute("SELECT id, estado FROM versiones_parametros")}
    assert estados == {v1["id"]: "retirada", v2["id"]: "activa"}
    with pytest.raises(P.ParametrosNoValidos):
        P.aprobar(con, v1["id"], "Daniel")                    # una retirada no vuelve sin nueva propuesta


@pytest.mark.parametrize("ratio, sens", [
    ([{"desde": "07:00", "g_por_u": 10}], [{"desde": "00:00", "mgdl_por_u": 40}]),     # no empieza a las 00:00
    ([{"desde": "00:00", "g_por_u": 1}], [{"desde": "00:00", "mgdl_por_u": 40}]),      # errata
    ([{"desde": "00:00", "g_por_u": 10}], [{"desde": "00:00", "mgdl_por_u": 400}]),
    ([{"desde": "00:00", "g_por_u": 10}, {"desde": "00:00", "g_por_u": 9}], [{"desde": "00:00", "mgdl_por_u": 40}]),
])
def test_parametros_no_validos(con, ratio, sens):
    with pytest.raises(P.ParametrosNoValidos):
        P.proponer(con, ratio, sens, "prueba")


def test_franja_por_hora_local():
    franjas = [{"desde": "00:00", "g_por_u": 10}, {"desde": "20:00", "g_por_u": 12}]
    assert P.valor_en(franjas, "g_por_u", datetime(2026, 11, 19, 18, 59, tzinfo=timezone.utc)) == (10, "00:00")
    assert P.valor_en(franjas, "g_por_u", datetime(2026, 11, 19, 19, 0, tzinfo=timezone.utc)) == (12, "20:00")


def test_la_base_no_deja_activar_sin_aprobacion(con):
    v = _parametros(con, aprobar=False)
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute("UPDATE versiones_parametros SET estado = 'activa' WHERE id = %s", (v["id"],))


# ------------------------------------------------------------------ cálculo con la base
def test_sugerencia_completa_y_guardada(con, ahora):
    _parametros(con)
    _glucosa(con, ahora, 150)
    con.execute("""INSERT INTO dosis (ts, unidades, insulina, origen, estado_conciliacion)
                   VALUES (%s, 2, 'fiasp', 'pluma_libreview', 'solo_pluma')""", (ahora - timedelta(hours=4),))
    s = SG.sugerir(con, ahora=ahora, hidratos_g=60, grasa_g=20, proteina_g=25, solicitud_lectura_id=_lectura_ok(con))
    assert s["estado_pluma"] == "leida" and not s["bloqueada"]
    assert 0 < float(s["insulina_activa_u"]) < 0.5
    assert float(s["u_inmediata"]) == 6.5                     # 6 + 1 − ~0,2 = 6,8 → 6,5
    assert float(s["u_segunda"]) == 1.0 and s["ts_segunda"] == ahora + timedelta(minutes=90)
    assert s["traza"]["franjas"] in ({"ratio_desde": "00:00", "sensibilidad_desde": "00:00"},
                                     {"ratio_desde": "20:00", "sensibilidad_desde": "00:00"})
    assert s["traza"]["insulina_activa"]["curva"]["version"]


def test_sin_parametros_queda_guardada_y_bloqueada(con, ahora):
    _glucosa(con, ahora)
    s = SG.sugerir(con, ahora=ahora, hidratos_g=40, solicitud_lectura_id=_lectura_ok(con))
    assert s["bloqueada"] and s["u_inmediata"] is None and s["version_parametros_id"] is None
    assert "parámetros" in s["motivo_bloqueo"]


def test_pluma_sin_leer_bloquea_salvo_que_confirme_las_dosis(con, ahora):
    _parametros(con)
    _glucosa(con, ahora)
    fallida = con.execute("""INSERT INTO solicitudes_lectura_pluma (atendida_en, resultado, detalle)
                             VALUES (now(), 'sin_sesion', 'pide SMS') RETURNING id""").fetchone()["id"]
    s = SG.sugerir(con, ahora=ahora, hidratos_g=40, solicitud_lectura_id=fallida)
    assert s["bloqueada"] and "pluma" in s["motivo_bloqueo"]
    s = SG.sugerir(con, ahora=ahora, hidratos_g=40)                       # ni siquiera se pidió
    assert s["bloqueada"] and s["estado_pluma"] == "no_leida"
    s = SG.sugerir(con, ahora=ahora, hidratos_g=40, solicitud_lectura_id=fallida, dosis_apuntadas=True)
    assert not s["bloqueada"] and s["estado_pluma"] == "confirmada_a_mano"
    assert s["incertidumbre"]["nivel"] != "baja"


def test_lectura_en_curso_hace_esperar_y_la_vieja_no_vale(con, ahora):
    _parametros(con)
    _glucosa(con, ahora)
    abierta = con.execute("INSERT INTO solicitudes_lectura_pluma DEFAULT VALUES RETURNING id").fetchone()["id"]
    with pytest.raises(SG.LecturaEnCurso):
        SG.sugerir(con, ahora=ahora, hidratos_g=40, solicitud_lectura_id=abierta)
    vieja = con.execute("""INSERT INTO solicitudes_lectura_pluma (pedida_en, atendida_en, resultado)
                           VALUES (now() - interval '30 min', now() - interval '29 min', 'ok') RETURNING id""").fetchone()["id"]
    assert SG.sugerir(con, ahora=ahora, hidratos_g=40, solicitud_lectura_id=vieja)["estado_pluma"] == "no_leida"


def test_glucosa_bajando_bloquea(con, ahora):
    _parametros(con)
    _glucosa(con, ahora, valor=110, pendiente=-1.4)
    s = SG.sugerir(con, ahora=ahora, hidratos_g=40, solicitud_lectura_id=_lectura_ok(con))
    assert s["bloqueada"] and s["u_inmediata"] is None


def test_comida_sin_revisar_no_vale(con, ahora):
    _parametros(con)
    _glucosa(con, ahora)
    c = con.execute("""INSERT INTO comidas (ts, origen, hidratos_g, hidratos_bolo_g, rango_hidratos_min_g,
                         rango_hidratos_max_g, grasa_g, proteina_g, revisada)
                       VALUES (%s, 'foto', 55, 50, 40, 65, 10, 20, false) RETURNING id""", (ahora,)).fetchone()["id"]
    with pytest.raises(SG.PeticionNoValida):
        SG.sugerir(con, ahora=ahora, comida_id=c, solicitud_lectura_id=_lectura_ok(con))
    con.execute("UPDATE comidas SET revisada = true WHERE id = %s", (c,))
    s = SG.sugerir(con, ahora=ahora, comida_id=c, hidratos_g=999, solicitud_lectura_id=_lectura_ok(con))
    assert float(s["hidratos_g"]) == 50 and s["comida_id"] == c      # manda la comida revisada
    assert s["incertidumbre"]["rango_u"] == [5.0, 7.5]


def test_las_sugerencias_no_se_modifican(con, ahora):
    _parametros(con)
    _glucosa(con, ahora)
    s = SG.sugerir(con, ahora=ahora, hidratos_g=40, solicitud_lectura_id=_lectura_ok(con))
    with pytest.raises(psycopg.errors.RaiseException):
        con.execute("UPDATE sugerencias SET u_inmediata = 10 WHERE id = %s", (s["id"],))


def test_segunda_inyeccion_a_su_hora(con, ahora):
    _parametros(con)
    comida = ahora - timedelta(minutes=95)
    _glucosa(con, ahora - timedelta(minutes=95))
    s = SG.sugerir(con, ahora=comida, ts_comida=comida, hidratos_g=60, grasa_g=20, proteina_g=25,
                   solicitud_lectura_id=_lectura_ok(con), dosis_apuntadas=True)
    assert float(s["u_segunda"]) == 1.0
    _glucosa(con, ahora, valor=140)
    seg = SG.segunda(con, s["id"], ahora=ahora, solicitud_lectura_id=_lectura_ok(con))
    assert seg["tipo"] == "segunda" and seg["sugerencia_origen_id"] == s["id"]
    assert float(seg["u_inmediata"]) == 1.0 and not seg["bloqueada"]
    with pytest.raises(SG.PeticionNoValida):
        SG.segunda(con, seg["id"], ahora=ahora)


def test_segunda_con_glucosa_baja_se_anula(con, ahora):
    _parametros(con)
    comida = ahora - timedelta(minutes=90)
    _glucosa(con, comida)
    s = SG.sugerir(con, ahora=comida, ts_comida=comida, hidratos_g=60, grasa_g=30, proteina_g=20,
                   dosis_apuntadas=True)
    _glucosa(con, ahora, valor=76)
    seg = SG.segunda(con, s["id"], ahora=ahora, dosis_apuntadas=True)
    assert seg["bloqueada"] and seg["u_inmediata"] is None


# ------------------------------------------------------------------ API
@pytest.fixture
def cli(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert _alta(c, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    return c


def test_api_exige_sesion(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert c.post("/api/sugerencias", json={"hidratos_g": 40}).status_code == 401
    assert c.get("/api/parametros").status_code == 401


def test_api_sugerencia(cli, con, ahora):
    _parametros(con)
    _glucosa(con, ahora)
    r = cli.post("/api/sugerencias", json={"hidratos_g": 60, "solicitud_lectura_id": _lectura_ok(con)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["u_inmediata"] == 7.0 and j["incertidumbre"]["nivel"] == "media"
    assert cli.get(f"/api/sugerencias/{j['id']}").json()["u_inmediata"] == 7.0
    assert cli.get("/api/parametros").json()["activa"]["ratio_hc"][0]["g_por_u"] == 10


def test_api_no_acepta_unidades_de_fuera(cli, con, ahora):
    r = cli.post("/api/sugerencias", json={"hidratos_g": 60, "u_inmediata": 12})
    assert r.status_code == 422


def test_api_espera_la_lectura(cli, con, ahora):
    _parametros(con)
    _glucosa(con, ahora)
    abierta = con.execute("INSERT INTO solicitudes_lectura_pluma DEFAULT VALUES RETURNING id").fetchone()["id"]
    assert cli.post("/api/sugerencias", json={"hidratos_g": 60, "solicitud_lectura_id": abierta}).status_code == 409
