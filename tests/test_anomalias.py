"""Servicio de anomalías con la base real y ntfy simulado; API de respuestas y evaluación."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import crear_app
from comun import parametros as P
from herramientas.ntfy_simulado import crear_app as crear_ntfy
from servicios.anomalias import avisos as A
from servicios.anomalias import detector as D
from servicios.anomalias import evaluar as E
from servicios.anomalias import reglas as R
from tests.autenticador_software import Autenticador
from tests.test_api import ORIGEN, _alta

AHORA = datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc)     # 12:00 en Madrid
U = R.Umbrales()


def _lecturas(con, valores, hasta=AHORA, paso=5):
    n = len(valores)
    for i, v in enumerate(valores):
        con.execute("INSERT INTO glucosa (ts, valor_mgdl) VALUES (%s, %s)", (hasta - timedelta(minutes=paso * (n - 1 - i)), v))


def _parametros(con, sens=45):
    v = P.proponer(con, [{"desde": "00:00", "g_por_u": 8}], [{"desde": "00:00", "mgdl_por_u": sens}], "prueba")
    return P.aprobar(con, v["id"], "Daniel")


def _dosis(con, ts, u=4, **k):
    return con.execute("""INSERT INTO dosis (ts, unidades, insulina, origen, id_externo, comida_id)
                          VALUES (%s, %s, 'fiasp', 'app', %s, %s) RETURNING id""",
                       (ts, u, f"t{ts.timestamp()}", k.get("comida_id"))).fetchone()["id"]


@pytest.fixture
def ntfy():
    app = crear_ntfy(token="tok")
    cli = A.ClienteNtfy("http://ntfy.test", "tema-secreto", "tok", cliente=TestClient(app, base_url="http://ntfy.test"))
    cli.app = app
    return cli


def _ciclo(con, ntfy, ahora=AHORA, u=U):
    r = D.ciclo(con, ahora, u)
    r["avisos"] = A.enviar_pendientes(con, ntfy, ahora, u, "https://app.test")
    return r


# ------------------------------------------------------------------ hueco
def test_hueco_abierto_avisa_una_vez_y_se_cierra(con, ntfy):
    _lecturas(con, [120] * 6, hasta=AHORA - timedelta(minutes=40))
    r = _ciclo(con, ntfy)
    assert len(r["nuevos"]) == 1 and r["avisos"]["enviados"] == 1
    m = ntfy.app.state.mensajes[0]
    assert m["topic"] == "tema-secreto" and m["title"] == "No llegan lecturas" and m["priority"] == 3
    assert m["click"] == "https://app.test/#avisos"
    assert _ciclo(con, ntfy, AHORA + timedelta(minutes=5))["nuevos"] == []      # el mismo hueco
    con.execute("INSERT INTO glucosa (ts, valor_mgdl) VALUES (%s, 118)", (AHORA + timedelta(minutes=7),))
    _ciclo(con, ntfy, AHORA + timedelta(minutes=10))
    a = con.execute("SELECT * FROM anomalias").fetchone()
    assert a["fin"] == AHORA + timedelta(minutes=7) and a["datos"]["abierto"] is False and "entre las" in a["texto"]
    assert len(ntfy.app.state.mensajes) == 1


# ------------------------------------------------------------------ Fiasp sin comida
def test_dosis_sin_comida_pregunta_una_vez(con, ntfy):
    _lecturas(con, [130] * 30)
    sola = _dosis(con, AHORA - timedelta(minutes=90))
    c = con.execute("INSERT INTO comidas (ts, origen) VALUES (%s, 'manual') RETURNING id", (AHORA - timedelta(minutes=300),)).fetchone()
    _dosis(con, AHORA - timedelta(minutes=80), comida_id=c["id"])
    _ciclo(con, ntfy)
    _ciclo(con, ntfy, AHORA + timedelta(minutes=5))
    filas = con.execute("SELECT * FROM anomalias WHERE tipo = 'dosis_sin_comida'").fetchall()
    assert len(filas) == 1 and filas[0]["dosis_id"] == sola
    assert filas[0]["datos"]["unidades"] == 4.0


def test_la_comida_del_chat_tambien_explica_la_dosis(con, ntfy):
    _lecturas(con, [130] * 30)
    _dosis(con, AHORA - timedelta(minutes=90))
    con.execute("""INSERT INTO eventos_chat (texto, tipo, ts_evento_inicio, estado) VALUES ('un bocadillo', 'comida', %s, 'pendiente')""",
                (AHORA - timedelta(minutes=100),))
    _ciclo(con, ntfy)
    assert con.execute("SELECT count(*) AS n FROM anomalias WHERE tipo = 'dosis_sin_comida'").fetchone()["n"] == 0


# ------------------------------------------------------------------ acumulación
def test_acumulacion_urgente_de_noche_y_repite_a_los_60_min(con, ntfy):
    noche = datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc)      # 03:00 en Madrid
    _parametros(con)
    _lecturas(con, [140] * 12, hasta=noche)
    _dosis(con, noche - timedelta(minutes=30), u=4)
    r = _ciclo(con, ntfy, noche)
    assert r["avisos"]["enviados"] == 1
    m = ntfy.app.state.mensajes[0]
    assert m["priority"] == 5 and m["title"] == "Acumulación de insulina"
    a = con.execute("SELECT * FROM anomalias WHERE tipo = 'acumulacion'").fetchone()
    assert a["urgente"] and a["datos"]["exceso_u"] > 3
    _lecturas(con, [140], hasta=noche + timedelta(minutes=30))
    n_acu = lambda: con.execute("SELECT count(*) AS n FROM anomalias WHERE tipo = 'acumulacion'").fetchone()["n"]
    _ciclo(con, ntfy, noche + timedelta(minutes=30))
    assert n_acu() == 1                                   # sigue la misma: no se repite antes de 60 min
    _lecturas(con, [140], hasta=noche + timedelta(minutes=61))
    _ciclo(con, ntfy, noche + timedelta(minutes=61))
    assert n_acu() == 2


def test_acumulacion_no_avisa_si_la_comida_la_cubre_o_sin_parametros(con, ntfy):
    _lecturas(con, [150] * 12)
    _dosis(con, AHORA - timedelta(minutes=20), u=6)
    _ciclo(con, ntfy)
    assert con.execute("SELECT count(*) AS n FROM anomalias WHERE tipo = 'acumulacion'").fetchone()["n"] == 0   # sin parámetros
    _parametros(con)
    con.execute("""INSERT INTO comidas (ts, origen, hidratos_g, hidratos_bolo_g, revisada, revisada_en)
                   VALUES (%s, 'manual', 60, 60, true, now())""", (AHORA - timedelta(minutes=25),))
    _ciclo(con, ntfy, AHORA + timedelta(minutes=1))
    assert con.execute("SELECT count(*) AS n FROM anomalias WHERE tipo = 'acumulacion'").fetchone()["n"] == 0


# ------------------------------------------------------------------ olvido de Tresiba
def test_olvido_de_tresiba_una_vez_al_dia(con, ntfy):
    con.execute("""INSERT INTO config_tresiba (vigente_desde, unidades, hora_local, hora_fin_local)
                   VALUES (%s, 34, '08:00', '11:00')""", (AHORA - timedelta(days=10),))
    _lecturas(con, [200 + i for i in range(60)])
    _ciclo(con, ntfy)
    _lecturas(con, [262], hasta=AHORA + timedelta(minutes=10))
    _ciclo(con, ntfy, AHORA + timedelta(minutes=10))
    filas = con.execute("SELECT * FROM anomalias WHERE tipo = 'olvido_tresiba'").fetchall()
    assert len(filas) == 1 and filas[0]["pregunta"] == "¿Te has puesto hoy la Tresiba?"


# ------------------------------------------------------------------ notificaciones
def test_silencio_nocturno_y_limite_diario(con, ntfy):
    noche = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)      # 02:00 en Madrid
    _lecturas(con, [120] * 6, hasta=noche - timedelta(minutes=40))
    r = _ciclo(con, ntfy, noche)
    assert r["nuevos"] and r["avisos"]["enviados"] == 0
    a = con.execute("SELECT * FROM anomalias").fetchone()
    assert a["avisar_desde"].astimezone(R.ZONA).hour == 8
    assert A.enviar_pendientes(con, ntfy, datetime(2026, 11, 20, 7, 5, tzinfo=timezone.utc), U)["enviados"] == 1

    u1 = R.Umbrales(max_avisos_dia=1)
    _dosis(con, datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc))
    _lecturas(con, [120] * 3, hasta=datetime(2026, 11, 20, 7, 10, tzinfo=timezone.utc))
    r = _ciclo(con, ntfy, datetime(2026, 11, 20, 7, 10, tzinfo=timezone.utc), u1)
    assert r["avisos"]["retenidos"] == 1 and len(ntfy.app.state.mensajes) == 1


def test_respondida_antes_de_enviar_no_se_envia_y_ntfy_caido_reintenta(con, ntfy):
    noche = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)
    _lecturas(con, [120] * 6, hasta=noche - timedelta(minutes=40))
    D.ciclo(con, noche, U)
    con.execute("UPDATE anomalias SET estado = 'respondida', respuesta = 'movil', respondida_en = now()")
    assert A.enviar_pendientes(con, ntfy, noche + timedelta(hours=7), U)["no_enviar"] == 1

    con.execute("TRUNCATE anomalias")
    D.ciclo(con, AHORA, U)
    ntfy.app.state.fallar = True
    assert A.enviar_pendientes(con, ntfy, AHORA, U)["errores"] == 1
    a = con.execute("SELECT * FROM anomalias").fetchone()
    assert a["aviso_estado"] == "pendiente" and a["aviso_intentos"] == 1
    assert con.execute("SELECT ntfy_error FROM estado_anomalias").fetchone()["ntfy_error"]
    ntfy.app.state.fallar = False
    assert A.enviar_pendientes(con, ntfy, AHORA + timedelta(minutes=5), U)["enviados"] == 1


def test_sin_ntfy_se_queda_en_la_app(con):
    _lecturas(con, [120] * 6, hasta=AHORA - timedelta(minutes=40))
    D.ciclo(con, AHORA, U)
    assert A.enviar_pendientes(con, None, AHORA, U)["enviados"] == 0
    assert A.enviar_pendientes(con, None, AHORA + timedelta(hours=13), U)["retenidos"] == 1   # ya llegaría tarde


def test_desactivar_un_tipo(con, ntfy):
    _lecturas(con, [120] * 6, hasta=AHORA - timedelta(minutes=40))
    assert _ciclo(con, ntfy, u=R.Umbrales(desactivadas=("hueco",)))["nuevos"] == []


# ------------------------------------------------------------------ API
@pytest.fixture
def cli(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert _alta(c, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    return c


def test_api_exige_sesion(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert c.get("/api/anomalias").status_code == 401
    assert c.post("/api/episodios", json={}).status_code == 401


def test_api_responder_y_panel(cli, con):
    ahora = datetime.now(timezone.utc)
    _lecturas(con, [120] * 6, hasta=ahora - timedelta(minutes=40))
    D.ciclo(con, ahora, U)
    assert cli.get("/api/panel").json()["anomalias_abiertas"] == 1
    r = cli.get("/api/anomalias").json()
    a = r["abiertas"][0]
    assert a["tipo"] == "hueco" and {o["clave"] for o in a["opciones"]} >= {"movil", "no_se"}
    assert cli.post(f"/api/anomalias/{a['id']}/respuesta", json={"respuesta": "inventada"}).status_code == 422
    assert cli.post(f"/api/anomalias/{a['id']}/respuesta", json={"respuesta": "movil", "nota": "en el gimnasio"}).status_code == 200
    r = cli.get("/api/anomalias").json()
    assert r["abiertas"] == [] and r["recientes"][0]["respuesta"] == "movil"
    assert cli.get("/api/panel").json()["anomalias_abiertas"] == 0
    assert cli.post(f"/api/anomalias/{a['id']}/descartar").json()["estado"] == "descartada"
    assert cli.post("/api/anomalias/9999/descartar").status_code == 404


def test_api_episodios(cli):
    ini = datetime.now(timezone.utc) - timedelta(days=3)
    r = cli.post("/api/episodios", json={"tipo": "presion", "inicio": ini.isoformat(),
                                         "fin": (ini + timedelta(minutes=40)).isoformat(), "notas": "dormido"})
    assert r.status_code == 200
    assert cli.post("/api/episodios", json={"tipo": "alcohol", "inicio": ini.isoformat(), "fin": ini.isoformat()}).status_code == 422
    assert cli.post("/api/episodios", json={"tipo": "presion", "inicio": ini.isoformat(),
                                            "fin": (ini - timedelta(minutes=1)).isoformat()}).status_code == 422
    assert len(cli.get("/api/episodios").json()["episodios"]) == 1
    assert cli.post(f"/api/episodios/{r.json()['id']}/borrar").json() == {"ok": True}
    assert cli.get("/api/episodios").json()["episodios"] == []


# ------------------------------------------------------------------ evaluación
def test_evaluacion_con_episodios_etiquetados(con):
    caida = [140, 140, 138, 115, 90, 78, 95, 120, 132, 136, 138]
    base = AHORA - timedelta(days=1)
    _lecturas(con, [140] * 40 + caida + [138] * 40, hasta=base)
    t_caida = base - timedelta(minutes=5 * (40 + len(caida) - 4))
    con.execute("INSERT INTO episodios_etiquetados (tipo, inicio, fin) VALUES ('presion', %s, %s)",
                (t_caida - timedelta(minutes=20), t_caida + timedelta(minutes=20)))
    con.execute("INSERT INTO episodios_etiquetados (tipo, inicio, fin) VALUES ('presion', %s, %s)",
                (base - timedelta(hours=20), base - timedelta(hours=19)))       # no hay nada ahí
    r = E.evaluar(con, base - timedelta(days=1), base)
    p = r["por_tipo"]["presion"]
    assert p["etiquetados"] == 2 and p["detectados"] == 1 and p["deteccion_pct"] == 50.0 and p["sin_etiqueta"] == 0
    assert r["total"]["cumple"] is False and len(p["no_detectados"]) == 1


def test_episodio_ya_terminado_no_se_notifica(con, ntfy):
    """Al arrancar el servicio no avisa por el móvil de una subida que acabó hace horas."""
    subida = [100] * 12 + [100 + 6 * i for i in range(1, 19)] + [200] * 3
    _lecturas(con, subida + [130] * 36, hasta=AHORA)            # el pico fue hace unas 3 h
    r = _ciclo(con, ntfy)
    a = con.execute("SELECT * FROM anomalias WHERE tipo = 'subida_sin_comida'").fetchone()
    assert a is not None and a["aviso_estado"] == "retenido" and a["estado"] == "abierta"
    assert r["avisos"]["enviados"] == 0 and ntfy.app.state.mensajes == []
