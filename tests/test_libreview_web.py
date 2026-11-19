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


def _leer(con, c, **kw):
    """Como la app: pide una lectura y el recolector da una vuelta."""
    E.pedir_lectura(con)
    return vuelta(con, c, **kw)


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


def test_sincronizacion_de_la_pluma_es_hora_local(lv, ruta):
    """Visto con la cuenta real (19-11-2026): uploadDate es la hora de Madrid contada como UTC."""
    c = _cliente(lv, ruta)
    local_como_utc = int(datetime(2026, 11, 19, 19, 9, tzinfo=timezone.utc).timestamp())
    _, pluma = c.cuerpo_informe({"id": "u"}, {"dataSources": {
        "y": {"id": "p", "type": 90002, "sn": "AB", "uploadDate": local_como_utc}}}, date(2026, 11, 19))
    assert pluma["sincronizacion"] == datetime(2026, 11, 19, 18, 9, tzinfo=timezone.utc)


# ------------------------------------------------------------------ flujo con el SMS
def test_flujo_completo_sms_desde_la_app(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    # 1. Sin dispositivo de confianza LibreView pide el SMS: se avisa, pero no se manda solo.
    assert _leer(con, c, ahora=ahora)["accion"] == "necesita_codigo"
    assert E.para_la_app(con, ahora)["accion"] == "pedir_sms" and lv.sms_enviados == 0
    assert _leer(con, c, ahora=ahora)["accion"] == "esperando_a_daniel" and lv.sms_enviados == 0
    # 2. Daniel pulsa "Enviarme el SMS": el recolector lo manda una sola vez.
    E.pedir_sms(con)
    assert _leer(con, c, ahora=ahora)["accion"] == "sms_enviado"
    _leer(con, c, ahora=ahora)
    assert lv.sms_enviados == 1
    app = E.para_la_app(con, ahora)
    assert app["accion"] == "codigo" and "0123" in app["mensaje"] and "600000" not in app["mensaje"]
    # 3. Escribe el código: el recolector entra, lo borra y lee las dosis.
    E.dar_codigo(con, " 123 456 ")
    r = _leer(con, c, ahora=ahora)
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
    _leer(con, c, ahora=ahora); E.pedir_sms(con); _leer(con, c, ahora=ahora)
    E.dar_codigo(con, "123456"); _leer(con, c, ahora=ahora)
    lv.tokens.clear()                                   # el token web caducó en LibreView
    c2 = _cliente(lv, ruta)                             # recolector reiniciado: lee la sesión del disco
    r = _leer(con, c2, ahora=ahora + timedelta(minutes=10))
    assert r["accion"] == "sincronizado" and r["dosis"]["insertadas"] == 0 and r["dosis"]["ya_estaban"] == 2
    assert lv.sms_enviados == 1
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 2


def test_dos_inyecciones_en_una_hora_quedan_aparte(con, ruta, ahora):
    t = _local_hace(ahora, hours=3).replace(minute=5)
    lv = LibreViewSimulado(password=CLAVE, inyecciones=[(t, 2), (t.replace(minute=40), 3)])
    c = _cliente(lv, ruta)
    _leer(con, c, ahora=ahora); E.pedir_sms(con); _leer(con, c, ahora=ahora)
    E.dar_codigo(con, "123456")
    r = _leer(con, c, ahora=ahora)
    assert r["dosis"]["insertadas"] == 0 and r["dosis"]["horas_ambiguas"] == 1
    h = con.execute("SELECT * FROM horas_pluma_libreview").fetchone()
    assert h["unidades_total"] == Decimal("5.00") and len(h["inyecciones"]) == 2
    _leer(con, c, ahora=ahora)                         # releer actualiza, no duplica
    assert con.execute("SELECT count(*) AS n FROM horas_pluma_libreview").fetchone()["n"] == 1


def test_codigo_equivocado_vuelve_a_pedir_sms(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    _leer(con, c, ahora=ahora); E.pedir_sms(con); _leer(con, c, ahora=ahora)
    E.dar_codigo(con, "999999")
    assert _leer(con, c, ahora=ahora)["accion"] == "codigo_rechazado"
    e = E.leer(con)
    assert e["estado"] == "necesita_codigo" and e["codigo"] is None and "código" in e["error"]
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 0


def test_libreview_caido_queda_en_el_estado_sin_secretos(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    _leer(con, c, ahora=ahora); E.pedir_sms(con); _leer(con, c, ahora=ahora)
    E.dar_codigo(con, "123456"); _leer(con, c, ahora=ahora)
    lv.fallar_informe = True
    r = _leer(con, c, ahora=ahora + timedelta(minutes=10))
    assert r["accion"] == "error"
    e = E.leer(con)
    assert e["estado"] == "error" and "500" in e["error"]
    assert CLAVE not in e["error"] and "web-" not in e["error"] and "confianza-" not in e["error"]
    lv.fallar_informe = False                           # se recupera solo en la vuelta siguiente
    assert _leer(con, c, ahora=ahora + timedelta(minutes=20))["accion"] == "sincronizado"
    assert E.leer(con)["estado"] == "ok"


def test_contrasena_equivocada_no_pide_sms(con, lv, ruta, ahora):
    c = ClienteLibreView("yo@ejemplo.test", "otra", ruta_sesion=ruta,
                         cliente=httpx.Client(transport=lv.transporte()))
    assert _leer(con, c, ahora=ahora)["accion"] == "error"
    assert "contraseña" in E.leer(con)["error"] and lv.sms_enviados == 0


def test_sin_lectura_pedida_no_llama_a_libreview(con, lv, ruta, ahora):
    E.poner(con, "ok", ultimo_ok=ahora)
    assert vuelta(con, _cliente(lv, ruta), ahora=ahora)["accion"] == "nada"
    assert lv.peticiones == []


def _dentro(con, c, ahora):
    _leer(con, c, ahora=ahora); E.pedir_sms(con); vuelta(con, c, ahora=ahora)
    E.dar_codigo(con, "123456")
    return vuelta(con, c, ahora=ahora)


# ------------------------------------------------------------------ lectura bajo demanda
def test_la_solicitud_se_cierra_con_su_resultado(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    s1 = E.pedir_lectura(con)
    vuelta(con, c, ahora=ahora)                         # LibreView pide el SMS: no hace esperar
    assert E.solicitud(con, s1["id"])["resultado"] == "sin_sesion"
    E.pedir_sms(con); vuelta(con, c, ahora=ahora); E.dar_codigo(con, "123456"); vuelta(con, c, ahora=ahora)
    s2, s3 = E.pedir_lectura(con), E.pedir_lectura(con)   # dos a la vez: una sola lectura
    n = lv.informes
    vuelta(con, c, ahora=ahora)
    assert lv.informes == n + 1
    assert {E.solicitud(con, s["id"])["resultado"] for s in (s2, s3)} == {"ok"}
    lv.fallar_informe = True
    s4 = E.pedir_lectura(con)
    vuelta(con, c, ahora=ahora)
    f = E.solicitud(con, s4["id"])
    assert f["resultado"] == "error" and f["atendida_en"] is not None and CLAVE not in f["detalle"]


def test_la_hora_en_curso_cuenta_ya(con, ruta):
    """19-11-2026: 2 U puestas a las 19:10 no contaban hasta las 20:00 y la insulina activa salía 0."""
    ahora = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
    en_curso = ahora.astimezone(MADRID).replace(minute=10, tzinfo=None)
    lv = LibreViewSimulado(password=CLAVE, inyecciones=[(en_curso - timedelta(hours=2), 4), (en_curso, 3)])
    c = _cliente(lv, ruta)
    r = _dentro(con, c, ahora)
    assert r["dosis"]["insertadas"] == 2                # también la de las :10 de esta hora
    # Otra inyección en esa misma hora: la guardada no se toca, se anota para revisar.
    lv.inyecciones = lv.inyecciones + [(en_curso + timedelta(minutes=15), 1)]
    r = _leer(con, c, ahora=ahora + timedelta(minutes=20))
    assert r["dosis"]["insertadas"] == 0 and r["cambios"] == 1
    assert {f["motivo"] for f in con.execute("SELECT motivo FROM cambios_pluma_libreview")} == {"otra_inyeccion"}
    assert [float(f["unidades"]) for f in con.execute("SELECT unidades FROM dosis ORDER BY ts")] == [4.0, 3.0]


def test_id_no_depende_de_las_unidades(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    _dentro(con, c, ahora)
    lv.inyecciones = [(t, u + 1) for t, u in lv.inyecciones]   # LibreView corrige los totales
    r = _leer(con, c, ahora=ahora)
    assert r["dosis"]["insertadas"] == 0 and r["cambios"] == 2
    # La dosis guardada no se toca: solo se anota para revisar.
    assert [f["unidades"] for f in con.execute("SELECT unidades FROM dosis ORDER BY ts")] == [Decimal("6.00"), Decimal("2.50")]
    assert {f["motivo"] for f in con.execute("SELECT motivo FROM cambios_pluma_libreview")} == {"total_distinto"}


def test_cebado_marcado_despues_y_otra_inyeccion_solo_se_anotan(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    _dentro(con, c, ahora)
    (t1, _), (t2, u2) = lv.inyecciones
    lv.inyecciones = [(t2, u2), (t2.replace(minute=(t2.minute + 30) % 60) if t2.minute < 30 else t2.replace(minute=0), 1)]
    r = _leer(con, c, ahora=ahora)
    assert r["dosis"]["insertadas"] == 0                # la segunda de esa hora no se deduce restando
    motivos = {f["motivo"] for f in con.execute("SELECT motivo FROM cambios_pluma_libreview")}
    assert motivos == {"desaparecida", "otra_inyeccion"}
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 2
    assert _leer(con, c, ahora=ahora)["cambios"] == 0   # ya anotados: no se repiten


# ------------------------------------------------------------------ estado para la app
def test_sms_caducado_y_lector_parado(con, ahora):
    E.poner(con, "esperando_codigo")                    # cambiado_en = now() de la base
    assert E.para_la_app(con)["accion"] == "codigo"
    assert E.para_la_app(con, datetime.now(timezone.utc) + timedelta(minutes=11))["accion"] == "pedir_sms"
    E.poner(con, "error", error="HTTP 500")
    E.pedir_lectura(con)
    a = E.para_la_app(con)
    assert a["accion"] == "esperar" and a["leyendo"] is True       # la nueva lectura va antes que el error viejo
    a = E.para_la_app(con, datetime.now(timezone.utc) + timedelta(minutes=3))
    assert a["accion"] is None and "500" in a["mensaje"]
    E.poner(con, "ok")
    assert E.para_la_app(con, datetime.now(timezone.utc) + timedelta(minutes=3))["estado"] == "sin_lector"
    E.cerrar_solicitudes(con, "ok")
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
    assert c.post("/api/libreview/actualizar").status_code == 401
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


def test_api_actualizar(cli, con):
    E.poner(con, "ok")
    r = cli.post("/api/libreview/actualizar").json()
    assert r["accion"] == "esperar" and r["leyendo"] is True
    assert cli.get(f"/api/libreview/actualizar/{r['solicitud']}").json()["resultado"] is None
    E.cerrar_solicitudes(con, "ok")
    assert cli.get(f"/api/libreview/actualizar/{r['solicitud']}").json()["resultado"] == "ok"
    assert cli.get("/api/libreview/actualizar/999").status_code == 404


def test_sms_rechazado_deja_reintentar_y_dice_el_motivo(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    lv.fallar_sms = True
    _leer(con, c, ahora=ahora); E.pedir_sms(con)
    assert vuelta(con, c, ahora=ahora)["accion"] == "sms_fallido"
    e = E.leer(con)
    assert e["estado"] == "necesita_codigo" and "HTTP 400" in e["error"] and "RegionMismatch" in e["error"]
    assert "yo@ejemplo.test" not in e["error"]
    app = E.para_la_app(con)
    assert app["accion"] == "pedir_sms" and "RegionMismatch" in app["mensaje"]
    with pytest.raises(E.EstadoNoPermite, match="Espera"):   # sin insistir (LibreView da 429)
        E.pedir_sms(con)
    con.execute("UPDATE estado_libreview SET cambiado_en = now() - interval '3 minutes'")
    lv.fallar_sms = False                               # pasado el rato, se puede volver a pedir
    E.pedir_sms(con)
    assert vuelta(con, c, ahora=ahora)["accion"] == "sms_enviado"


def test_pedir_sms_entra_de_nuevo_y_no_reutiliza_un_token_viejo(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    _leer(con, c, ahora=ahora)                          # deja guardado un token parcial
    lv.parciales.clear()                                # LibreView lo da por caducado
    c2 = _cliente(lv, ruta)                             # recolector reiniciado, con la sesión del disco
    logins = lv.logins
    E.pedir_sms(con)
    assert vuelta(con, c2, ahora=ahora)["accion"] == "sms_enviado"
    assert lv.logins == logins + 1 and lv.sms_enviados == 1


def test_si_libreview_deja_entrar_sin_sms_lee_directamente(con, lv, ruta, ahora):
    c = _cliente(lv, ruta)
    _dentro(con, c, ahora)                              # ya hay dispositivo de confianza
    E.poner(con, "necesita_codigo")                     # p. ej. un aviso viejo
    E.pedir_sms(con)
    r = vuelta(con, c, ahora=ahora)
    assert r["accion"] == "sincronizado" and lv.sms_enviados == 1   # solo el del primer día
    assert E.leer(con)["estado"] == "ok"


def test_token_de_chrome_entra_sin_sms(con, lv, ruta, ahora):
    lv.confianza_valida.add("confianza-de-chrome")
    c = ClienteLibreView("yo@ejemplo.test", CLAVE, ruta_sesion=ruta, espera_sondeo_s=0,
                         dispositivo_confianza="confianza-de-chrome",
                         cliente=httpx.Client(transport=lv.transporte()))
    r = _leer(con, c, ahora=ahora)
    assert r["accion"] == "sincronizado" and r["dosis"]["insertadas"] == 2 and lv.sms_enviados == 0
