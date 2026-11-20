"""Mis platos: la misma estimación para el mismo plato (fase 3).

La coincidencia es por texto y conservadora; el valor del plato sigue pasando por la revisión de
Daniel antes de una dosis (regla 1). Datos sintéticos; nunca se llama a Ollama.
"""
from datetime import datetime, timezone

import pytest

from comun import platos as P
from servicios.comidas import chat as CH
from servicios.comidas import trabajador as T
from tests.test_chat import resp
from tests.test_comidas import _foto, cli, falso, fotos, salida_prototipo  # noqa: F401  (fixtures)


# ------------------------------------------------------------------ coincidencia (pura)
def _p(nombre, **k):
    return {"id": 1, "nombre": nombre, "clave": P.clave(nombre), **k}


def test_clave_quita_tildes_signos_y_espacios():
    assert P.clave("  Bocadillo de  JAMÓN. ") == "bocadillo de jamon"


@pytest.mark.parametrize("texto, sale", [
    ("bocadillo de jamón", True),
    ("Bocadillo de jamon.", True),
    ("bocadilo de jamón", True),                 # errata
    ("bocadillo de jamón grande", False),        # la ración puede ser otra
    ("medio bocadillo de jamón", False),
    ("bocadillo de queso", False),
    ("pan", False),
    ("", False),
])
def test_elegir_es_conservador(texto, sale):
    assert (P.elegir(texto, [_p("bocadillo de jamón")]) is not None) is sale


def test_mencionado_en_una_frase_gana_el_mas_largo():
    platos = [_p("bocadillo"), _p("bocadillo de jamón")]
    assert P.mencionado("me he comido un bocadillo de jamón hace una hora", platos)["nombre"] == "bocadillo de jamón"
    assert P.mencionado("un bocadillo de jamonero", [_p("bocadillo de jamón")]) is None   # palabras enteras
    assert P.mencionado("me he comido una manzana", platos) is None


# ------------------------------------------------------------------ guardar al revisar
def _texto(cli, texto, idc):
    return cli.post("/api/comidas/texto", json={"id_cliente": idc, "texto": texto}).json()


def _lista_texto(cli, con, texto, idc, hc=62.0):
    c = _texto(cli, texto, idc)
    T.vuelta(con, None, falso(salida_prototipo(hc=hc, lo=hc - 10, hi=hc + 10)))
    return c["id"]


def test_revisar_con_nombre_guarda_el_plato_y_sin_nombre_no(cli, con):
    a = _lista_texto(cli, con, "bocadillo de jamón", "plato-cli-1")
    cli.post(f"/api/comidas/{a}/revisar", json={"hidratos_g": 45, "plato": "Bocadillo de jamón"})
    b = _lista_texto(cli, con, "lentejas", "plato-cli-2")
    cli.post(f"/api/comidas/{b}/revisar", json={})
    platos = cli.get("/api/platos").json()["platos"]
    assert [(p["nombre"], p["hidratos_g"], p["veces"], p["referencia"]) for p in platos] == \
        [("Bocadillo de jamón", 45, 1, False)]


def test_un_plato_pesado_no_lo_pisa_una_comida_sin_pesar(cli, con):
    a = _lista_texto(cli, con, "yogur con avena", "plato-cli-3")
    cli.post(f"/api/comidas/{a}/revisar", json={"hidratos_g": 30, "referencia": True, "plato": "yogur con avena"})
    b = _lista_texto(cli, con, "yogur con avena", "plato-cli-4")
    cli.post(f"/api/comidas/{b}/revisar", json={"hidratos_g": 50, "plato": "yogur con avena"})
    p = cli.get("/api/platos").json()["platos"][0]
    assert (p["hidratos_g"], p["referencia"], p["veces"]) == (30, True, 2)
    # Uno nuevo sin pesar sí se actualiza con lo último confirmado.
    c = _lista_texto(cli, con, "tostada", "plato-cli-5")
    cli.post(f"/api/comidas/{c}/revisar", json={"hidratos_g": 20, "plato": "tostada con tomate"})
    d = _lista_texto(cli, con, "tostada", "plato-cli-6")
    cli.post(f"/api/comidas/{d}/revisar", json={"hidratos_g": 25, "plato": "tostada con tomate"})
    t = next(p for p in cli.get("/api/platos").json()["platos"] if p["nombre"] == "tostada con tomate")
    assert (t["hidratos_g"], t["veces"]) == (25, 2)


def test_borrar_un_plato(cli, con):
    a = _lista_texto(cli, con, "paella", "plato-cli-7")
    cli.post(f"/api/comidas/{a}/revisar", json={"hidratos_g": 70, "plato": "paella"})
    pid = cli.get("/api/platos").json()["platos"][0]["id"]
    assert cli.post(f"/api/platos/{pid}/borrar", json={}).status_code == 200
    assert cli.get("/api/platos").json()["platos"] == []
    assert cli.post(f"/api/platos/{pid}/borrar", json={}).status_code == 404


def test_platos_exige_sesion(ajustes, con):
    from fastapi.testclient import TestClient

    from api.main import crear_app
    from tests.test_api import ORIGEN
    anon = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert anon.get("/api/platos").status_code == 401
    assert anon.post("/api/platos/1/borrar", json={}).status_code == 401


# ------------------------------------------------------------------ el trabajador lo reutiliza
@pytest.fixture
def plato(cli, con):
    a = _lista_texto(cli, con, "bocadillo de jamón", "plato-cli-10")
    cli.post(f"/api/comidas/{a}/revisar", json={"hidratos_g": 48, "plato": "bocadillo de jamón"})
    return cli.get("/api/platos").json()["platos"][0]


def test_texto_igual_da_lo_mismo_sin_preguntar_a_los_modelos(cli, con, plato):
    c = _texto(cli, "Bocadillo de jamón", "plato-cli-11")
    a = falso()
    T.vuelta(con, None, a)
    v = cli.get(f"/api/comidas/{c['id']}").json()
    assert a.llamadas == []
    assert v["hidratos_g"] == 48 and (v["hidratos_min_g"], v["hidratos_max_g"]) == (48, 48)
    assert v["plato_id"] == plato["id"] and v["hidratos_modelo_g"] is None and not v["revisada"]
    assert "Como la última vez" in v["alertas"][0]


def test_foto_con_el_mismo_nombre_usa_el_plato_y_compara_con_los_modelos(cli, con, plato):
    c = _foto(cli, id_cliente="plato-foto-1", nota="bocadillo de jamón").json()
    a = falso(salida_prototipo(hc=75, lo=60, hi=80))
    T.vuelta(con, None, a)
    v = cli.get(f"/api/comidas/{c['id']}").json()
    assert len(a.llamadas) == 1                                    # los modelos sí miran la foto
    assert v["hidratos_g"] == 48 and v["hidratos_modelo_g"] == 75 and v["plato_id"] == plato["id"]
    assert (v["hidratos_min_g"], v["hidratos_max_g"]) == (48, 80)
    assert "Los modelos ven 75 g" in v["alertas"][0] and "ración" in v["alertas"][0]


def test_foto_con_otra_nota_no_usa_el_plato(cli, con, plato):
    c = _foto(cli, id_cliente="plato-foto-2", nota="bocadillo de jamón grande").json()
    T.vuelta(con, None, falso())
    v = cli.get(f"/api/comidas/{c['id']}").json()
    assert v["hidratos_g"] == 62 and v["plato_id"] is None and v["hidratos_modelo_g"] == 62


# ------------------------------------------------------------------ chat
def _chat(cli, con, texto, idc, respuesta, hidratos_tabla=None):
    e = cli.post("/api/chat", json={"id_cliente": idc, "texto": texto}).json()
    CH.vuelta(con, lambda t: respuesta, "m", hidratos_tabla)
    return cli.get(f"/api/chat/{e['id']}").json()["estimacion"]


def test_chat_usa_tu_plato_si_lo_nombra(cli, con, plato):
    est = _chat(cli, con, "me he comido un bocadillo de jamón hace una hora", "plato-chat-1",
                resp(tipo="comida", hidratos_min_g=10, hidratos_max_g=20, bebidas=None))
    assert est["hidratos_g"] == [48, 48] and est["fuente_hidratos"] == "plato" and est["plato"] == "bocadillo de jamón"


def test_chat_sin_plato_usa_la_tabla_y_si_no_el_modelo(cli, con):
    alimentos = [{"nombre": "paella", "nombre_en": "paella", "gramos": 300}]
    est = _chat(cli, con, "paella", "plato-chat-2", resp(tipo="comida", alimentos=alimentos, bebidas=None),
                hidratos_tabla=lambda al: 60.0 if al == alimentos else None)
    assert est["hidratos_g"] == [48, 72] and est["fuente_hidratos"] == "tabla"
    est = _chat(cli, con, "algo raro", "plato-chat-3", resp(tipo="comida", alimentos=[], bebidas=None),
                hidratos_tabla=lambda al: None)
    assert est["hidratos_g"] == [20, 45] and est["fuente_hidratos"] == "modelo"


def test_chat_de_ejercicio_no_lleva_hidratos(cli, con, plato):
    est = _chat(cli, con, "salgo a correr en media hora tras el bocadillo de jamón", "plato-chat-4",
                resp(tipo="ejercicio", hidratos_min_g=None, hidratos_max_g=None, bebidas=None, etiquetas=[]))
    assert "hidratos_g" not in est and "fuente_hidratos" not in est


class TablaFalsa:
    usda = []

    def buscar(self, nombre_es, nombre_en):
        filas = {"arroz": {"nombre": "arroz", "fuente": "propio", "hidratos": 30.0, "fibra": 1.0, "azucares": 0.0,
                           "grasa": 0.0, "proteina": 2.0, "kcal": 130.0}}
        f = filas.get(nombre_es)
        return (f, 100) if f else (None, 0)


def test_hidratos_tabla_solo_si_estan_todos():
    assert T.hidratos_tabla(TablaFalsa(), [{"nombre": "arroz", "nombre_en": "rice", "gramos": 200}]) == 58.0  # 60 − 2 de fibra
    assert T.hidratos_tabla(TablaFalsa(), [{"nombre": "arroz", "gramos": 200}, {"nombre": "kebab", "gramos": 300}]) is None
    assert T.hidratos_tabla(TablaFalsa(), [{"nombre": "arroz", "gramos": 0}]) is None
    assert T.hidratos_tabla(TablaFalsa(), []) is None


def test_fecha_del_aviso_en_hora_de_madrid():
    assert T._fecha(datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc)) == "21-11"
