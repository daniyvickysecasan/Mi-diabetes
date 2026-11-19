"""Lectura de la pluma desde la web de LibreView (simulada), con el código del SMS desde la app.

Todo sintético: herramientas/libreview_simulado.py. Nunca se llama a LibreView de verdad.
"""
import json
import os
import stat
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import crear_app
from comun import estado_libreview as E
from herramientas.libreview_simulado import LibreViewSimulado, datos_informe, html_informe
from servicios.recoleccion.libreview import (ClienteLibreView, ErrorLibreView, dosis_del_informe,
                                             extraer_datos)
from servicios.recoleccion.sondeo_libreview import vuelta
from tests.autenticador_software import Autenticador
from tests.test_api import ORIGEN, _alta

MADRID = ZoneInfo("Europe/Madrid")
CLAVE = "clave-sintetica"


def _local_hace(ahora, **kw):
    return (ahora - timedelta(**kw)).astimezone(MADRID).replace(second=0, microsecond=0)


@pytest.fixture
def lv(ahora):
    return LibreViewSimulado(password=CLAVE, sondeos_503=2, inyecciones=[
        (_local_hace(ahora, hours=5), 6),
        (_local_hace(ahora, hours=2), 2.5),
    ])


def _cliente(lv, ruta):
    return ClienteLibreView("yo@ejemplo.test", CLAVE, ruta_sesion=ruta, espera_sondeo_s=0,
                            cliente=httpx.Client(transport=lv.transporte()))


@pytest.fixture
def ruta(tmp_path):
    return tmp_path / "libreview" / "sesion.json"


# ------------------------------------------------------------------ lectura del informe
def test_extraer_datos_aguanta_llaves_dentro_de_textos():
    datos = datos_informe([(datetime(2026, 1, 10, 13, 37), 4)])
    assert extraer_datos(html_informe(datos)) == json.loads(json.dumps(datos))


def test_extraer_datos_sin_json_da_error_claro():
    with pytest.raises(ErrorLibreView, match="formato"):
        extraer_datos("<html>nada</html>")


def test_una_inyeccion_por_hora_es_una_dosis_con_hora_de_la_pluma():
    datos = datos_informe([(datetime(2026, 1, 10, 13, 37), 4.5),       # invierno: UTC+1
                           (datetime(2026, 7, 10, 8, 5), 3)])          # verano: UTC+2
    lect = dosis_del_informe(datos, MADRID, "PLUMAS")
    assert [(d.ts, d.unidades) for d in lect.dosis] == [
        (datetime(2026, 1, 10, 12, 37, tzinfo=timezone.utc), Decimal("4.50")),
        (datetime(2026, 7, 10, 6, 5, tzinfo=timezone.utc), Decimal("3.00"))]
    assert {d.serie_pluma for d in lect.dosis} == {"PLUMAS"}
    assert lect.horas_ambiguas == []


def test_dos_inyecciones_en_la_misma_hora_no_se_reparten():
    datos = datos_informe([(datetime(2026, 1, 10, 14, 5), 2), (datetime(2026, 1, 10, 14, 50), 3),
                           (datetime(2026, 1, 10, 20, 0), 5)])
    lect = dosis_del_informe(datos, MADRID)
    assert [d.unidades for d in lect.dosis] == [Decimal("5.00")]       # solo la de las 20:00
    (h,) = lect.horas_ambiguas
    assert h["hora"] == datetime(2026, 1, 10, 13, 0, tzinfo=timezone.utc)
    assert h["unidades_total"] == Decimal("5.00") and len(h["inyecciones"]) == 2


def test_hora_sin_unidades_no_inventa_dosis():
    datos = datos_informe([(datetime(2026, 1, 10, 9, 0), 2)])
    for h in datos["Days"][0]["Hours"]:
        h["RapidActingInsulinManual"] = 0
    lect = dosis_del_informe(datos, MADRID)
    assert lect.dosis == [] and lect.sin_unidades == 1


def test_cuerpo_del_informe_como_la_web(lv, ruta):
    c = _cliente(lv, ruta)
    cuerpo, pluma = c.cuerpo_informe({"id": "u", "country": "ES", "uiLanguage": "es"},
                                     {"dataSources": {"x": {"id": "s", "type": 40066},
                                                      "y": {"id": "p", "type": 90002, "sn": "AB", "uploadDate": 10}}},
                                     date(2026, 11, 19))
    assert cuerpo["EndDate"] == cuerpo["TodayDate"] == int(datetime(2026, 11, 19, tzinfo=timezone.utc).timestamp())
    assert cuerpo["StartDates"] == [cuerpo["EndDate"] - 13 * 86400]
    assert cuerpo["PrimaryDeviceId"] == "s" and cuerpo["ConnectedInsulinDeviceIds"] == ["p"]
    assert cuerpo["CultureCode"] == "es-ES" and pluma["serie"] == "AB"


# ------------------------------------------------------------------ flujo con el SMS
def test_flujo_completo_sms_desde_la_app(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    # 1. Sin dispositivo de confianza LibreView pide el SMS: se avisa, pero no se manda solo.
    assert vuelta(con, c, ahora=ahora)["accion"] == "necesita_codigo"
    assert E.para_la_app(con, ahora)["accion"] == "pedir_sms" and lv.sms_enviados == 0
    assert vuelta(con, c, ahora=ahora)["accion"] == "esperando_a_daniel" and lv.sms_enviados == 0
    # 2. Daniel pulsa "Enviarme el SMS": el recolector lo manda una sola vez.
    E.pedir_sms(con)
    assert vuelta(con, c, ahora=ahora)["accion"] == "sms_enviado"
    vuelta(con, c, ahora=ahora)
    assert lv.sms_enviados == 1
    app = E.para_la_app(con, ahora)
    assert app["accion"] == "codigo" and "0123" in app["mensaje"] and "600000" not in app["mensaje"]
    # 3. Escribe el código: el recolector entra, lo borra y lee las dosis.
    E.dar_codigo(con, " 123 456 ")
    r = vuelta(con, c, ahora=ahora)
    assert r["accion"] == "sincronizado" and r["dosis"]["insertadas"] == 2
    e = E.leer(con)
    assert e["estado"] == "ok" and e["codigo"] is None and e["ultimo_ok"] == ahora
    assert e["ultima_sincronizacion"] is not None
    filas = con.execute("SELECT ts, unidades, origen, numero_serie FROM dosis ORDER BY ts").fetchall()
    assert [f["unidades"] for f in filas] == [Decimal("6.00"), Decimal("2.50")]
    assert {f["origen"] for f in filas} == {"pluma_libreview"} and {f["numero_serie"] for f in filas} == {"PLUMAS"}
    assert filas[1]["ts"] == _local_hace(ahora, hours=2).astimezone(timezone.utc)
    # 4. La sesión se guarda solo para el usuario y sin la contraseña.
    assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600
    guardado = ruta.read_text()
    assert CLAVE not in guardado and "confianza-" in guardado and "yo@ejemplo.test" not in guardado


def test_tras_reiniciar_entra_sin_sms_y_no_duplica(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    vuelta(con, c, ahora=ahora); E.pedir_sms(con); vuelta(con, c, ahora=ahora)
    E.dar_codigo(con, "123456"); vuelta(con, c, ahora=ahora)
    lv.tokens.clear()                                   # el token web caducó en LibreView
    c2 = _cliente(lv, ruta)                             # recolector reiniciado: lee la sesión del disco
    r = vuelta(con, c2, ahora=ahora + timedelta(minutes=10))
    assert r["accion"] == "sincronizado" and r["dosis"]["insertadas"] == 0 and r["dosis"]["ya_estaban"] == 2
    assert lv.sms_enviados == 1
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 2


def test_dos_inyecciones_en_una_hora_quedan_aparte(con, ruta, ahora):
    t = _local_hace(ahora, hours=3).replace(minute=5)
    lv = LibreViewSimulado(password=CLAVE, inyecciones=[(t, 2), (t.replace(minute=40), 3)])
    c = _cliente(lv, ruta)
    vuelta(con, c, ahora=ahora); E.pedir_sms(con); vuelta(con, c, ahora=ahora)
    E.dar_codigo(con, "123456")
    r = vuelta(con, c, ahora=ahora)
    assert r["dosis"]["insertadas"] == 0 and r["dosis"]["horas_ambiguas"] == 1
    h = con.execute("SELECT * FROM horas_pluma_libreview").fetchone()
    assert h["unidades_total"] == Decimal("5.00") and len(h["inyecciones"]) == 2
    vuelta(con, c, ahora=ahora)                         # releer actualiza, no duplica
    assert con.execute("SELECT count(*) AS n FROM horas_pluma_libreview").fetchone()["n"] == 1


def test_codigo_equivocado_vuelve_a_pedir_sms(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    vuelta(con, c, ahora=ahora); E.pedir_sms(con); vuelta(con, c, ahora=ahora)
    E.dar_codigo(con, "999999")
    assert vuelta(con, c, ahora=ahora)["accion"] == "codigo_rechazado"
    e = E.leer(con)
    assert e["estado"] == "necesita_codigo" and e["codigo"] is None and "código" in e["error"]
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 0


def test_libreview_caido_queda_en_el_estado_sin_secretos(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    vuelta(con, c, ahora=ahora); E.pedir_sms(con); vuelta(con, c, ahora=ahora)
    E.dar_codigo(con, "123456"); vuelta(con, c, ahora=ahora)
    lv.fallar_informe = True
    r = vuelta(con, c, ahora=ahora + timedelta(minutes=10))
    assert r["accion"] == "error"
    e = E.leer(con)
    assert e["estado"] == "error" and "500" in e["error"]
    assert CLAVE not in e["error"] and "web-" not in e["error"] and "confianza-" not in e["error"]
    lv.fallar_informe = False                           # se recupera solo en la vuelta siguiente
    assert vuelta(con, c, ahora=ahora + timedelta(minutes=20))["accion"] == "sincronizado"
    assert E.leer(con)["estado"] == "ok"


def test_contrasena_equivocada_no_pide_sms(con, lv, ruta, ahora):
    c = ClienteLibreView("yo@ejemplo.test", "otra", ruta_sesion=ruta,
                         cliente=httpx.Client(transport=lv.transporte()))
    assert vuelta(con, c, ahora=ahora)["accion"] == "error"
    assert "contraseña" in E.leer(con)["error"] and lv.sms_enviados == 0


def test_sin_sincronizar_no_llama_a_libreview(con, lv, ruta, ahora):
    E.poner(con, "ok", ultimo_ok=ahora)
    assert vuelta(con, _cliente(lv, ruta), sincronizar=False, ahora=ahora)["accion"] == "nada"
    assert lv.peticiones == []


# ------------------------------------------------------------------ estado para la app
def test_sms_caducado_y_sondeo_caido(con, ahora):
    E.poner(con, "esperando_codigo")                    # cambiado_en = now() de la base
    assert E.para_la_app(con)["accion"] == "codigo"
    assert E.para_la_app(con, datetime.now(timezone.utc) + timedelta(minutes=11))["accion"] == "pedir_sms"
    E.poner(con, "ok", ultimo_ok=ahora - timedelta(hours=2))
    a = E.para_la_app(con, datetime.now(timezone.utc))
    assert a["estado"] == "caido" and a["mensaje"]
    E.poner(con, "ok", ultimo_ok=datetime.now(timezone.utc))
    assert E.para_la_app(con)["mensaje"] is None


def test_codigo_solo_cifras_y_solo_si_se_espera(con):
    with pytest.raises(ValueError):
        E.dar_codigo(con, "12ab")
    with pytest.raises(E.EstadoNoPermite):
        E.dar_codigo(con, "123456")
    E.poner(con, "ok")
    with pytest.raises(E.EstadoNoPermite):
        E.pedir_sms(con)


# ------------------------------------------------------------------ API
@pytest.fixture
def cli(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert _alta(c, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    return c


def test_api_exige_sesion(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert c.get("/api/libreview/estado").status_code == 401
    assert c.post("/api/libreview/sms").status_code == 401
    assert c.post("/api/libreview/codigo", json={"codigo": "123456"}).status_code == 401


def test_api_sms_y_codigo(cli, con):
    E.poner(con, "necesita_codigo")
    assert cli.get("/api/libreview/estado").json()["accion"] == "pedir_sms"
    assert cli.post("/api/libreview/sms").json()["estado"] == "sms_pedido"
    assert cli.post("/api/libreview/codigo", json={"codigo": "123456"}).status_code == 409   # aún no hay SMS
    E.poner(con, "esperando_codigo", destino_sms="•••••••0123")
    assert cli.post("/api/libreview/codigo", json={"codigo": "12-34"}).status_code == 422
    r = cli.post("/api/libreview/codigo", json={"codigo": "654321"})
    assert r.status_code == 200 and r.json()["estado"] == "codigo_recibido" and "654321" not in r.text
    assert E.leer(con)["codigo"] == "654321"
    assert cli.post("/api/libreview/sms").status_code == 409            # ya está comprobando
