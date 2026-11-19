"""API de la fase 2: dosis, revisión, hidratos de corrección, Tresiba, panel. Reenvíos idempotentes."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import crear_app
from tests.autenticador_software import Autenticador
from tests.test_api import ORIGEN, _alta


@pytest.fixture
def cli(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert _alta(c, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    return c


def hace(min):
    return (datetime.now(timezone.utc) - timedelta(minutes=min)).isoformat()


def test_todo_exige_sesion(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    for ruta in ("/api/dosis", "/api/dosis/revisar", "/api/hidratos-correccion", "/api/tresiba",
                 "/api/insulina-activa", "/api/metricas", "/api/comidas/recientes"):
        assert c.get(ruta).status_code == 401
    assert c.post("/api/dosis", json={"id_cliente": "abcdefgh1", "ts": hace(0), "unidades": 2}).status_code == 401


def test_registrar_dosis_y_reenvio_idempotente(cli):
    cuerpo = {"id_cliente": "movil-0001", "ts": hace(30), "unidades": 4.5}
    r1 = cli.post("/api/dosis", json=cuerpo).json()
    r2 = cli.post("/api/dosis", json=cuerpo).json()          # la cola sin conexión reintenta
    assert r1["nueva"] is True and r2["nueva"] is False
    assert r1["dosis"]["id"] == r2["dosis"]["id"]
    assert r1["dosis"]["estado"] == "pendiente" and r1["dosis"]["cuenta"] is True
    assert len(cli.get("/api/dosis").json()["dosis"]) == 1
    iob = cli.get("/api/insulina-activa").json()
    assert 3 < iob["unidades"] < 4.5 and iob["con_dudas"] is True


def test_validaciones(cli):
    futura = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert cli.post("/api/dosis", json={"id_cliente": "movil-0002", "ts": futura, "unidades": 2}).status_code == 422
    assert cli.post("/api/dosis", json={"id_cliente": "movil-0003", "ts": hace(1), "unidades": 0}).status_code == 422
    sin_zona = datetime.now().replace(microsecond=0).isoformat()
    assert cli.post("/api/dosis", json={"id_cliente": "movil-0004", "ts": sin_zona, "unidades": 2}).status_code == 422
    assert cli.post("/api/dosis", json={"id_cliente": "movil-0005", "ts": hace(1), "unidades": 2,
                                        "comida_id": 999}).status_code == 422


def test_revision_desde_la_api(cli, con):
    a = cli.post("/api/dosis", json={"id_cliente": "movil-0010", "ts": hace(60), "unidades": 4}).json()["dosis"]
    con.execute("""INSERT INTO dosis (ts, unidades, insulina, origen, id_externo)
                   VALUES (%s, 5, 'fiasp', 'pluma_libreview', 'lv-1')""",
                (datetime.fromisoformat(a["ts"]) + timedelta(minutes=4),))
    from comun.conciliacion import conciliar
    conciliar(con)
    rev = cli.get("/api/dosis/revisar").json()["dosis"]
    assert len(rev) == 1 and rev[0]["estado"] == "discrepancia" and rev[0]["par"]["unidades"] in (4, 5)
    assert cli.get("/api/panel").json()["por_revisar"] == 1
    assert cli.post(f"/api/dosis/{a['id']}/revision", json={"accion": "inventada"}).status_code == 422
    assert cli.post(f"/api/dosis/{a['id']}/revision", json={"accion": "aceptar"}).status_code == 200
    assert cli.get("/api/dosis/revisar").json()["dosis"] == []
    iob = cli.get("/api/insulina-activa").json()
    assert [d["origen"] for d in iob["dosis"]] == ["pluma_libreview"]


def test_comida_va_a_la_fila_de_la_app(cli, con):
    cid = con.execute("INSERT INTO comidas (ts, origen) VALUES (now(), 'manual') RETURNING id").fetchone()["id"]
    a = cli.post("/api/dosis", json={"id_cliente": "movil-0020", "ts": hace(5), "unidades": 3}).json()["dosis"]
    p = con.execute("""INSERT INTO dosis (ts, unidades, insulina, origen, id_externo)
                       VALUES (%s, 3, 'fiasp', 'pluma_libreview', 'lv-2') RETURNING id""",
                    (datetime.fromisoformat(a["ts"]),)).fetchone()["id"]
    from comun.conciliacion import conciliar
    conciliar(con)
    r = cli.post(f"/api/dosis/{p}/comida", json={"comida_id": cid}).json()
    assert r["dosis_id"] == a["id"]
    assert con.execute("SELECT comida_id FROM dosis_efectivas").fetchone()["comida_id"] == cid
    assert cli.get("/api/comidas/recientes").json()["comidas"][0]["id"] == cid


def test_hidratos_de_correccion(cli):
    h = {"id_cliente": "movil-0030", "ts": hace(2), "gramos": 15, "motivo": "hipoglucemia"}
    assert cli.post("/api/hidratos-correccion", json=h).json()["nueva"] is True
    assert cli.post("/api/hidratos-correccion", json=h).json()["nueva"] is False
    assert [x["gramos"] for x in cli.get("/api/hidratos-correccion").json()["hidratos"]] == [15]
    assert cli.post("/api/hidratos-correccion", json={**h, "id_cliente": "movil-0031", "motivo": "x"}).status_code == 422


def test_tresiba(cli):
    assert cli.get("/api/tresiba").json()["vigente"] is None
    desde = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    r = cli.post("/api/tresiba", json={"unidades": 34, "hora_local": "08:00", "hora_fin_local": "08:00",
                                       "vigente_desde": desde})
    assert r.status_code == 200 and r.json()["vigente"]["unidades"] == 34
    assert cli.post("/api/tresiba", json={"unidades": 34, "hora_local": "08:00", "hora_fin_local": "08:00",
                                          "vigente_desde": desde}).status_code == 409
    t = cli.get("/api/tresiba").json()
    assert t["vigente"]["hora_local"] == "08:00" and len(t["programadas"]) in (2, 3)
    r = cli.post("/api/tresiba", json={"unidades": 34, "hora_local": "07:00", "hora_fin_local": "10:00"})
    assert r.json()["vigente"]["hora_fin_local"] == "10:00"
    assert "ts_estimada" in cli.get("/api/tresiba").json()["programadas"][0]
    assert cli.get("/api/panel").json()["tresiba"]["unidades"] == 34


def test_panel_trae_lo_de_la_fase_2(cli):
    p = cli.get("/api/panel").json()
    for k in ("insulina_activa", "metricas_14d", "por_revisar", "tresiba", "ultimas_dosis"):
        assert k in p
    assert p["insulina_activa"]["unidades"] == 0


def test_tresiba_sin_fin_de_franja_se_rechaza(cli):
    """Una versión vieja de la app (sin el campo "hasta") no debe guardar una franja de anchura cero."""
    r = cli.post("/api/tresiba", json={"unidades": 34, "hora_local": "08:00"})
    assert r.status_code == 422
    assert cli.get("/api/tresiba").json()["vigente"] is None


def test_pwa_se_revalida_siempre(cli):
    for ruta in ("/", "/app.js", "/sw.js"):
        assert cli.get(ruta).headers["cache-control"] == "no-cache"
    assert cli.get("/api/tresiba").headers["cache-control"] == "no-store"
