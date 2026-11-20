"""Registro tipo chat: interpretación (modelo simulado), API y regla 1. Nunca se llama a Ollama."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import crear_app
from servicios.comidas import chat as CH
from tests.autenticador_software import Autenticador
from tests.test_api import ORIGEN, _alta

T0 = datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc)


def resp(**k):
    base = {"tipo": "alcohol", "empieza_hace_min": 0, "dura_min": None, "hidratos_min_g": 20, "hidratos_max_g": 45,
            "bebidas": 3, "intensidad": None, "etiquetas": ["Fiesta", "alcohol"], "incertidumbre": "alta",
            "resumen": "Está tomando unas cervezas con amigos."}
    return base | k


# ------------------------------------------------------------------ normalizar (puro)
def test_alcohol_ahora():
    d = CH.normalizar(resp(), T0)
    assert d["tipo"] == "alcohol" and d["ts_evento_inicio"] == T0 and d["ts_evento_fin"] is None
    assert d["estimacion"] == {"hidratos_g": [20, 45], "bebidas": 3}
    assert d["etiquetas"] == ["alcohol", "fiesta"] and d["incertidumbre"] == "alta"


def test_ejercicio_en_el_futuro_con_duracion():
    d = CH.normalizar(resp(tipo="ejercicio", empieza_hace_min=-30, dura_min=45, intensidad="media",
                           etiquetas=[], incertidumbre="baja"), T0)
    assert d["ts_evento_inicio"] == T0 + timedelta(minutes=30) and d["ts_evento_fin"] == T0 + timedelta(minutes=75)
    assert d["estimacion"] == {"intensidad": "media", "duracion_min": 45}         # sin hidratos ni bebidas
    assert d["etiquetas"] == ["ejercicio"] and d["incertidumbre"] == "baja"


@pytest.mark.parametrize("texto, dicho, hace", [
    ("he comido una paella hace una hora", -60, 60),              # el fallo real de gemma3:27b
    ("hace 20 minutos me he comido una manzana", -20, 20),
    ("he comido una paella hace una hora", 60, 60),
    ("voy al gimnasio en media hora", 30, -30),
    ("dentro de 2 horas salgo a correr", 120, -120),
    ("me estoy tomando una cerveza", -15, -15),                   # sin pista: se respeta al modelo
    ("hace un rato, y en media hora vuelvo", -30, -30),            # ambigua: se respeta al modelo
])
def test_el_signo_del_tiempo_lo_pone_la_frase(texto, dicho, hace):
    d = CH.normalizar(resp(empieza_hace_min=dicho), T0, texto)
    assert d["ts_evento_inicio"] == T0 - timedelta(minutes=hace)


def test_futuro_demasiado_lejano_se_descarta_tras_corregir_el_signo():
    d = CH.normalizar(resp(empieza_hace_min=20 * 60), T0, "en 20 horas me voy de viaje")
    assert d["ts_evento_inicio"] is None


@pytest.mark.parametrize("cambios, esperado", [
    ({"tipo": "inventado"}, {"tipo": "otro"}),
    ({"empieza_hace_min": 99999}, {"ts_evento_inicio": None}),
    ({"empieza_hace_min": "ayer"}, {"ts_evento_inicio": None}),
    ({"dura_min": -5}, {"ts_evento_fin": None}),
    ({"incertidumbre": "nula"}, {"incertidumbre": "alta"}),
    ({"empieza_hace_min": None, "incertidumbre": "baja"}, {"incertidumbre": "media"}),
    ({"hidratos_min_g": 60, "hidratos_max_g": 30}, {"estimacion": {"hidratos_g": [30, 60], "bebidas": 3}}),
    ({"hidratos_max_g": 9000}, {"estimacion": {"bebidas": 3}}),
])
def test_lo_que_no_cuadra_se_deja_vacio(cambios, esperado):
    d = CH.normalizar(resp(**cambios), T0)
    assert {k: d[k] for k in esperado} == esperado


def test_etiquetas_limpias_y_sin_repetir():
    d = CH.normalizar(resp(etiquetas=[" ALCOHOL ", "", None, "x" * 50] + [f"e{i}" for i in range(20)]), T0)
    assert d["etiquetas"][0] == "alcohol" and len(d["etiquetas"]) == 8 and len(d["etiquetas"][1]) == 30


# ------------------------------------------------------------------ API + trabajador
@pytest.fixture
def cli(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert _alta(c, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    return c


def _nuevo(cli, texto="me estoy tomando unas cervezas con los amigos", idc="chat-cliente-1"):
    return cli.post("/api/chat", json={"id_cliente": idc, "texto": texto})


def test_flujo_completo(cli, con):
    r = _nuevo(cli)
    assert r.status_code == 200 and r.json()["analisis"] == "pendiente"
    assert _nuevo(cli).json()["id"] == r.json()["id"]                    # reenvío: no duplica
    textos = []
    out = CH.vuelta(con, lambda t: (textos.append(t), resp())[1], "modelo-x")
    assert out["estado"] == "lista" and textos == ["me estoy tomando unas cervezas con los amigos"]
    assert CH.vuelta(con, lambda t: resp(), "modelo-x") is None
    e = cli.get(f"/api/chat/{r.json()['id']}").json()
    assert e["tipo"] == "alcohol" and e["estimacion"]["bebidas"] == 3 and e["estado"] == "pendiente"
    c = cli.post(f"/api/chat/{e['id']}/confirmar", json={})
    assert c.status_code == 200 and c.json()["estado"] == "confirmado" and not c.json()["corregido"]
    assert cli.post(f"/api/chat/{e['id']}/descartar").status_code == 409
    assert [x["id"] for x in cli.get("/api/chat").json()["eventos"]] == [e["id"]]


def test_corregir_el_tipo_y_descartar(cli, con):
    a = _nuevo(cli, "salgo a dar una vuelta", "chat-cliente-2").json()
    b = _nuevo(cli, "nada, era broma", "chat-cliente-3").json()
    CH.vuelta(con, lambda t: resp(tipo="otro", etiquetas=[]), "m")
    CH.vuelta(con, lambda t: resp(tipo="otro", etiquetas=[]), "m")
    c = cli.post(f"/api/chat/{a['id']}/confirmar", json={"tipo": "ejercicio"}).json()
    assert c["tipo"] == "ejercicio" and c["corregido"] and c["etiquetas"][0] == "ejercicio"
    assert cli.post(f"/api/chat/{b['id']}/descartar").json()["estado"] == "descartado"
    assert [x["id"] for x in cli.get("/api/chat").json()["eventos"]] == [a["id"]]     # el descartado no sale
    assert cli.post(f"/api/chat/{a['id']}/confirmar", json={"tipo": "dosis"}).status_code == 422


def test_si_el_modelo_falla_se_elige_a_mano(cli, con):
    e = _nuevo(cli, "me duele la cabeza", "chat-cliente-4").json()

    def caido(texto):
        raise TimeoutError("Ollama no responde")
    assert CH.vuelta(con, caido, "m")["estado"] == "error"
    v = cli.get(f"/api/chat/{e['id']}").json()
    assert v["analisis"] == "error" and v["tipo"] is None
    assert cli.post(f"/api/chat/{e['id']}/confirmar", json={}).status_code == 422          # falta el tipo
    assert cli.post(f"/api/chat/{e['id']}/confirmar", json={"tipo": "enfermedad"}).json()["estado"] == "confirmado"


def test_el_trabajador_no_pisa_un_mensaje_ya_decidido(cli, con):
    e = _nuevo(cli, "café", "chat-cliente-5").json()
    fila = CH.tomar(con)
    cli.post(f"/api/chat/{e['id']}/confirmar", json={"tipo": "bebida"})
    CH.interpretar(con, fila, lambda t: resp(), "m")
    assert cli.get(f"/api/chat/{e['id']}").json()["tipo"] == "bebida"


def test_exige_sesion(ajustes, con):
    anon = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert anon.get("/api/chat").status_code == 401
    assert anon.post("/api/chat", json={"id_cliente": "chat-cliente-9", "texto": "hola"}).status_code == 401


# ------------------------------------------------------------------ sin aviso por alcohol (Daniel, 19-11-2026)
def test_no_hay_aviso_por_alcohol():
    import pathlib
    raiz = pathlib.Path(__file__).resolve().parent.parent
    for f in ("servicios/comidas/chat.py", "api/chat.py", "pwa/app.js"):
        texto = (raiz / f).read_text().lower()
        assert "hipoglucemia tardía" not in texto and "hipo tardía" not in texto
