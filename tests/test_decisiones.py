"""Decisión sobre una sugerencia y resultado a 3–4 h. AFECTA A LA DOSIS (crea dosis de la app)."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from api.main import crear_app
from comun import decisiones as D
from comun import sugerir as SG
from tests.autenticador_software import Autenticador
from tests.test_api import ORIGEN, _alta
from tests.test_sugerir import _glucosa, _lectura_ok, _parametros


@pytest.fixture
def ahora():
    return datetime.now(timezone.utc)


def _sug(con, ahora, **kw):
    _parametros(con)
    _glucosa(con, ahora, kw.pop("glucosa", 150))
    return SG.sugerir(con, ahora=ahora, hidratos_g=kw.pop("hidratos_g", 60), solicitud_lectura_id=_lectura_ok(con), **kw)


def test_hecho_crea_la_dosis_de_la_app(con, ahora):
    s = _sug(con, ahora)
    r = D.decidir(con, s["id"], "hecho", ts=ahora, id_cliente="movil-dec-1")
    d = con.execute("SELECT * FROM dosis WHERE id = %s", (r["decision"]["dosis_id"],)).fetchone()
    assert float(d["unidades"]) == 7.0 and d["origen"] == "app" and d["sugerencia_id"] == s["id"]
    assert d["estado_conciliacion"] == "pendiente"          # espera a la pluma, como cualquier dosis
    again = D.decidir(con, s["id"], "hecho", ts=ahora, id_cliente="movil-dec-1")
    assert again["nueva"] is False
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 1
    with pytest.raises(D.DecisionNoValida):
        D.decidir(con, s["id"], "ignorado", id_cliente="movil-dec-2")      # una sola decisión


def test_modificado_y_ignorado(con, ahora):
    s = _sug(con, ahora)
    with pytest.raises(D.DecisionNoValida):
        D.decidir(con, s["id"], "modificado", unidades=6.3)                # pasos de 0,5
    D.decidir(con, s["id"], "modificado", unidades=6.0, ts=ahora)
    assert float(con.execute("SELECT unidades FROM dosis").fetchone()["unidades"]) == 6.0
    s2 = SG.sugerir(con, ahora=ahora, hidratos_g=20, solicitud_lectura_id=_lectura_ok(con))
    r = D.decidir(con, s2["id"], "ignorado")
    assert r["decision"]["dosis_id"] is None and r["decision"]["unidades_reales"] is None


def test_bloqueada_no_se_puede_marcar_hecha(con, ahora):
    s = _sug(con, ahora, glucosa=75)
    assert s["bloqueada"]
    with pytest.raises(D.DecisionNoValida):
        D.decidir(con, s["id"], "hecho")
    D.decidir(con, s["id"], "modificado", unidades=0)                    # "no me he puesto nada"
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 0


def test_la_base_exige_coherencia(con, ahora):
    s = _sug(con, ahora)
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute("INSERT INTO decisiones (sugerencia_id, decision, unidades_reales) VALUES (%s, 'ignorado', 3)", (s["id"],))


def test_resultado_puro():
    t0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    lect = [{"ts": t0 + timedelta(minutes=m), "valor_mgdl": v} for m, v in
            [(0, 150), (60, 200), (120, 160), (178, 120), (240, 65), (245, 70)]]
    r = D.resultado(lect, t0)
    assert r["glucosa_3h"] == 120 and r["glucosa_4h"] == 65
    assert r["minima"] == 65 and r["maxima"] == 200 and r["hipo"] is True
    assert r["lecturas"] == 5                              # la de 4 h 5 min queda fuera
    assert D.resultado([], t0) == {"lecturas": 0}


def test_evaluar_pendientes(con, ahora):
    antes = ahora - timedelta(hours=5)
    s = _sug(con, antes)
    D.decidir(con, s["id"], "hecho", ts=antes)
    for m in range(0, 241, 5):
        con.execute("INSERT INTO glucosa (ts, valor_mgdl) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (antes + timedelta(minutes=m), 140))
    assert D.evaluar_pendientes(con, ahora) == 1
    f = con.execute("SELECT * FROM decisiones").fetchone()
    assert f["resultado"]["glucosa_3h"] == 140 and f["resultado"]["hipo"] is False
    assert D.evaluar_pendientes(con, ahora) == 0


# ------------------------------------------------------------------ API
@pytest.fixture
def cli(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert _alta(c, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    return c


def test_api_decision_y_listado(cli, con, ahora):
    s = _sug(con, ahora)
    assert cli.post(f"/api/sugerencias/{s['id']}/decision",
                    json={"id_cliente": "movil-dec-9", "decision": "hecho", "unidades": 12}).status_code == 422
    r = cli.post(f"/api/sugerencias/{s['id']}/decision", json={"id_cliente": "movil-dec-9", "decision": "hecho"})
    assert r.status_code == 200, r.text
    assert r.json()["sugerencia"]["decision"]["unidades"] == 7.0
    lista = cli.get("/api/sugerencias").json()["sugerencias"]
    assert len(lista) == 1 and lista[0]["decision"]["decision"] == "hecho"
    assert cli.get("/api/sugerencias").status_code == 200


def test_api_marca_la_segunda_comprobada(cli, con, ahora):
    comida = ahora - timedelta(minutes=90)
    _parametros(con)
    _glucosa(con, comida)
    s = SG.sugerir(con, ahora=comida, ts_comida=comida, hidratos_g=60, grasa_g=20, proteina_g=25, dosis_apuntadas=True)
    assert cli.get(f"/api/sugerencias/{s['id']}").json()["segunda_comprobada_id"] is None
    _glucosa(con, ahora)
    r = cli.post(f"/api/sugerencias/{s['id']}/segunda", json={"solicitud_lectura_id": _lectura_ok(con)})
    assert r.status_code == 200 and r.json()["u_inmediata"] == 1.0
    assert cli.get(f"/api/sugerencias/{s['id']}").json()["segunda_comprobada_id"] == r.json()["id"]
