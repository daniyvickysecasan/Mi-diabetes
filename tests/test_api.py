"""API: passkey, sesión obligatoria, glucosa y panel."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from api.main import crear_app
from comun import glucosa as G
from servicios.recoleccion.recolector import importar
from tests.autenticador_software import Autenticador

ORIGEN = "http://localhost:8000"


@pytest.fixture
def cliente(con, ajustes):
    return TestClient(crear_app(ajustes), base_url=ORIGEN)


def _alta(cliente, aut, codigo="codigo-de-prueba"):
    r = cliente.post("/auth/alta/opciones", json={"codigo_alta": codigo})
    assert r.status_code == 200, r.text
    o = r.json()
    return cliente.post("/auth/alta/verificar",
                        json={"id_reto": o["id_reto"], "credencial": aut.crear(o["opciones"]), "nombre": "iPhone"})


def _entrar(cliente, aut, uv=True):
    o = cliente.post("/auth/entrada/opciones").json()
    return cliente.post("/auth/entrada/verificar",
                        json={"id_reto": o["id_reto"], "credencial": aut.firmar(o["opciones"], uv=uv)})


def test_sin_sesion_no_hay_datos(cliente):
    for ruta in ("/api/glucosa/actual", "/api/glucosa", "/api/glucosa/tir", "/api/panel"):
        assert cliente.get(ruta).status_code == 401


def test_pwa_y_salud_publicas(cliente):
    r = cliente.get("/")
    assert r.status_code == 200 and "Mi Diabetes" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert cliente.get("/salud").json() == {"ok": True}


def test_alta_exige_codigo(cliente):
    assert cliente.post("/auth/alta/opciones", json={"codigo_alta": "mal"}).status_code == 403


def test_alta_desactivada_sin_codigo_configurado(con, ajustes):
    from dataclasses import replace
    c = TestClient(crear_app(replace(ajustes, codigo_alta="")), base_url=ORIGEN)
    assert c.post("/auth/alta/opciones", json={"codigo_alta": ""}).status_code == 403


def test_flujo_completo_de_passkey(cliente, ajustes):
    aut = Autenticador(ajustes.rp_id, ORIGEN)
    assert _alta(cliente, aut).status_code == 200
    assert cliente.get("/api/panel").status_code == 200          # el alta abre sesión
    assert cliente.post("/auth/salir").status_code == 200
    assert cliente.get("/api/panel").status_code == 401
    assert _entrar(cliente, aut).status_code == 200
    assert cliente.get("/auth/estado").json() == {"autenticado": True, "hay_passkeys": True}
    assert cliente.get("/api/panel").status_code == 200


def test_segunda_passkey_exige_sesion(cliente, ajustes):
    assert _alta(cliente, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    cliente.post("/auth/salir")
    r = cliente.post("/auth/alta/opciones", json={"codigo_alta": "codigo-de-prueba"})
    assert r.status_code == 403                                  # el código solo no basta


def test_passkey_desconocida_o_sin_verificacion(cliente, ajustes):
    aut = Autenticador(ajustes.rp_id, ORIGEN)
    _alta(cliente, aut)
    cliente.post("/auth/salir")
    assert _entrar(cliente, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 401
    assert _entrar(cliente, aut, uv=False).status_code == 401     # sin Face ID no entra


def test_origen_ajeno_rechazado(cliente, ajustes):
    assert _alta(cliente, Autenticador(ajustes.rp_id, "https://malo.example")).status_code == 400


def test_reto_de_un_solo_uso(cliente, ajustes):
    aut = Autenticador(ajustes.rp_id, ORIGEN)
    _alta(cliente, aut)
    cliente.post("/auth/salir")
    o = cliente.post("/auth/entrada/opciones").json()
    cred = aut.firmar(o["opciones"])
    assert cliente.post("/auth/entrada/verificar", json={"id_reto": o["id_reto"], "credencial": cred}).status_code == 200
    cliente.post("/auth/salir")
    assert cliente.post("/auth/entrada/verificar", json={"id_reto": o["id_reto"], "credencial": cred}).status_code == 400


def test_panel_con_datos(cliente, ajustes, con, ns, ahora):
    importar(con, ns, ahora - timedelta(days=16), ahora + timedelta(minutes=1), "historico")
    _alta(cliente, Autenticador(ajustes.rp_id, ORIGEN))
    p = cliente.get("/api/panel").json()
    assert p["actual"]["valor_mgdl"] > 0
    assert p["actual"]["flecha"]
    assert len(p["serie_24h"]) > 200
    for k in ("tir_24h", "tir_14d"):
        t = p[k]
        assert abs(t["pct_bajo"] + t["pct_en_rango"] + t["pct_alto"] - 100) < 0.5
        assert t["cobertura_pct"] > 80
    assert p["recoleccion"]["ultima"] is not None
    r = cliente.get("/api/glucosa/tir?horas=48").json()
    assert r["rango_mgdl"] == [70, 180]


def test_tir_por_tiempo_y_limites_incluidos(con, ahora):
    """70 y 180 cuentan en rango; un tramo con lecturas cada minuto no pesa más."""
    filas = []
    t0 = ahora - timedelta(hours=2)
    for i in range(12):                     # 1 h con lecturas cada 5 min a 70 y 180
        filas.append((t0 + timedelta(minutes=5 * i), 70 if i % 2 else 180))
    for i in range(60):                     # 1 h con lecturas cada minuto a 250
        filas.append((t0 + timedelta(hours=1, minutes=i), 250))
    for ts, v in filas:
        con.execute("INSERT INTO glucosa (ts, valor_mgdl) VALUES (%s, %s)", (ts, v))
    t = G.tir(con, t0, ahora)
    assert t["intervalos_5min"] == 24
    assert t["pct_en_rango"] == 50.0 and t["pct_alto"] == 50.0 and t["pct_bajo"] == 0.0


def test_actual_antigua_y_tendencia_calculada(con, ahora):
    con.execute("INSERT INTO glucosa (ts, valor_mgdl) VALUES (%s, 100), (%s, 115)",
                (ahora - timedelta(minutes=35), ahora - timedelta(minutes=30)))
    a = G.actual(con, ahora=ahora)
    assert a["antigua"] is True
    assert a["delta_5min_mgdl"] == 15.0
    assert a["tendencia"] == "SingleUp"
