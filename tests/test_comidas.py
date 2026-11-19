"""Comidas por foto o texto (fase 3): API, trabajador con un analizador simulado y paso a la dosis.

Nunca se llama a Ollama: `analizar_plato` se sustituye por una función con datos sintéticos.
La regla 1 se comprueba aquí: los gramos de los modelos no llegan a una dosis sin revisión.
"""
import ast
import base64
import dataclasses
import pathlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import crear_app
from comun import sugerir as SG
from servicios.comidas import trabajador as T
from tests.autenticador_software import Autenticador
from tests.test_api import ORIGEN, _alta
from tests.test_sugerir import _glucosa, _lectura_ok, _parametros

RATIO_FIJO = [{"desde": "00:00", "g_por_u": 10}]               # el mismo ratio a cualquier hora
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 300            # basta con la firma: nadie la decodifica aquí


def salida_prototipo(hc=62.0, lo=48.0, hi=70.0, bolo=None):
    item = {"nombre": "arroz blanco cocido", "gramos": 200.4, "fuente": "propios",
            "por100": {"hidratos": 28.0, "fibra": 0.4, "grasa": 0.3, "proteina": 2.7, "azucares": 0.1, "kcal": 130}}
    return {"origen": "foto", "consenso": {"hidratos": hc, "fibra": 1.0, "azucares": 2.0, "grasa": 12.0,
                                           "proteina": 30.0, "kcal": 480.0},
            "hc_bolo": bolo if bolo is not None else hc, "rango_hc": (lo, hi),
            "estimaciones": [("modelo-a/bd", {"hidratos": hc})], "items": [item],
            "alertas": ["Salsa no visible: puede haber más grasa"], "errores": [], "modelos_items": {"a": ["arroz"]}}


def falso(salida=None, fallo=None):
    llamadas = []

    def analizar_plato(tabla, imagen=None, nota="", texto=None, progreso=print):
        llamadas.append({"imagen": imagen, "nota": nota, "texto": texto})
        if fallo:
            raise RuntimeError(fallo)
        return salida or salida_prototipo()
    analizar_plato.llamadas = llamadas
    return analizar_plato


@pytest.fixture
def fotos(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "RUTA_FOTOS", tmp_path)
    return tmp_path


@pytest.fixture
def cli(con, ajustes, fotos):
    c = TestClient(crear_app(dataclasses.replace(ajustes, ruta_fotos=str(fotos))), base_url=ORIGEN)
    assert _alta(c, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    return c


def _foto(cli, id_cliente="foto-cliente-1", datos=JPEG, **kw):
    return cli.post("/api/comidas/foto", json={"id_cliente": id_cliente,
                                               "imagen_b64": "data:image/jpeg;base64," + base64.b64encode(datos).decode(), **kw})


# ------------------------------------------------------------------ API: alta
def test_subir_foto_la_guarda_pendiente_y_no_duplica(cli, con, fotos):
    r = _foto(cli, nota="sin salsa")
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["estado"] == "pendiente" and c["hidratos_g"] is None and not c["revisada"]
    f = con.execute("SELECT * FROM comidas").fetchone()
    assert (fotos / f["foto_ruta"]).read_bytes() == JPEG and f["descripcion"] == "sin salsa"
    assert _foto(cli).json()["id"] == c["id"]                               # reenvío del móvil
    assert con.execute("SELECT count(*) AS n FROM comidas").fetchone()["n"] == 1
    assert len(list(fotos.iterdir())) == 1


@pytest.mark.parametrize("datos", [b"GIF89a" + b"\x00" * 300, b"\xff\xd8\xff" + b"\x00" * (6 * 1024 * 1024)])
def test_rechaza_lo_que_no_es_foto_o_es_enorme(cli, con, datos):
    assert _foto(cli, datos=datos).status_code == 422
    assert con.execute("SELECT count(*) AS n FROM comidas").fetchone()["n"] == 0


def test_texto_y_sesion(cli, con, ajustes):
    r = cli.post("/api/comidas/texto", json={"id_cliente": "texto-cliente-1", "texto": " arroz con pollo "})
    assert r.status_code == 200 and r.json()["estado"] == "pendiente" and r.json()["descripcion"] == "arroz con pollo"
    anon = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert anon.get(f"/api/comidas/{r.json()['id']}").status_code == 401
    assert anon.post("/api/comidas/texto", json={"id_cliente": "texto-cliente-2", "texto": "pan"}).status_code == 401


# ------------------------------------------------------------------ trabajador
def test_trabajador_analiza_la_foto(cli, con, fotos):
    c = _foto(cli, nota="sin salsa").json()
    a = falso()
    r = T.vuelta(con, tabla=None, analizar_plato=a)
    assert r == {"id": c["id"], "estado": "lista", "hidratos_bolo_g": 62.0}
    assert a.llamadas[0]["imagen"] == JPEG and a.llamadas[0]["nota"] == "sin salsa"
    v = cli.get(f"/api/comidas/{c['id']}").json()
    assert v["estado"] == "lista" and v["hidratos_g"] == 62 and (v["hidratos_min_g"], v["hidratos_max_g"]) == (48, 70)
    assert v["items"][0] == {"nombre": "arroz blanco cocido", "gramos": 200, "hidratos": 56.1, "fibra": 0.8,
                             "grasa": 0.6, "proteina": 5.4, "fuente": "propios"}
    assert v["alertas"] and v["segundos"] is not None
    assert T.vuelta(con, tabla=None, analizar_plato=a) is None              # cola vacía


def test_trabajador_texto_y_error(cli, con):
    c = cli.post("/api/comidas/texto", json={"id_cliente": "texto-cliente-3", "texto": "lentejas"}).json()
    assert T.vuelta(con, None, falso(fallo="Ollama no responde"))["estado"] == "error"
    v = cli.get(f"/api/comidas/{c['id']}").json()
    assert v["estado"] == "error" and "Ollama" in v["error"] and v["hidratos_g"] is None
    # Sin estimación, "Correcto" no vale; escribiendo los hidratos sí.
    assert cli.post(f"/api/comidas/{c['id']}/revisar", json={}).status_code == 422
    assert cli.post(f"/api/comidas/{c['id']}/revisar", json={"hidratos_g": 40}).json()["hidratos_g"] == 40


def test_el_rango_siempre_incluye_el_valor_del_bolo():
    d = T.resumen(salida_prototipo(hc=60, lo=50, hi=58, bolo=59))
    assert d["rango_hidratos_min_g"] <= d["hidratos_bolo_g"] <= d["rango_hidratos_max_g"]


def test_tomar_no_reparte_la_misma_comida_dos_veces(con):
    for i in range(2):
        con.execute("""INSERT INTO comidas (ts, origen, descripcion, estado_analisis) VALUES (now(), 'texto', %s, 'pendiente')""",
                    (f"plato {i}",))
    a, b = T.tomar(con), T.tomar(con)
    assert a["id"] != b["id"] and T.tomar(con) is None


def test_el_trabajador_no_pisa_una_comida_ya_revisada(cli, con):
    c = cli.post("/api/comidas/texto", json={"id_cliente": "texto-cliente-4", "texto": "pasta"}).json()
    fila = T.tomar(con)
    cli.post(f"/api/comidas/{c['id']}/revisar", json={"hidratos_g": 55})
    T.analizar(con, fila, None, falso())
    assert cli.get(f"/api/comidas/{c['id']}").json()["hidratos_g"] == 55


# ------------------------------------------------------------------ revisión y dosis (regla 1)
@pytest.fixture
def lista(cli, con):
    c = _foto(cli).json()
    T.vuelta(con, None, falso())
    return c["id"]


def test_sin_revisar_no_hay_dosis(cli, con, lista):
    _parametros(con, ratio=RATIO_FIJO)
    _glucosa(con, datetime.now(timezone.utc))
    r = cli.post("/api/sugerencias", json={"tipo": "comida", "comida_id": lista,
                                           "solicitud_lectura_id": _lectura_ok(con), "dosis_apuntadas": True})
    assert r.status_code == 422 and "Revisa" in r.text
    assert con.execute("SELECT count(*) AS n FROM sugerencias").fetchone()["n"] == 0


def test_correcto_y_la_dosis_usa_los_gramos_y_el_rango(cli, con, lista):
    v = cli.post(f"/api/comidas/{lista}/revisar", json={}).json()
    assert v["revisada"] and not v["corregida"]
    _parametros(con, ratio=RATIO_FIJO)
    _glucosa(con, datetime.now(timezone.utc), 110)
    r = cli.post("/api/sugerencias", json={"tipo": "comida", "comida_id": lista,
                                           "solicitud_lectura_id": _lectura_ok(con), "dosis_apuntadas": True})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["comida_id"] == lista and s["hidratos_g"] == 62 and s["grasa_g"] == 12 and s["proteina_g"] == 30
    assert s["u_hidratos"] == 6.2 and s["u_inmediata"] == 6.0          # 62 / 10, redondeo hacia abajo
    assert s["incertidumbre"]["rango_u"] == [4.5, 7.0]              # 48–70 g
    # Revisar dos veces no cambia nada.
    assert cli.post(f"/api/comidas/{lista}/revisar", json={"hidratos_g": 10}).json()["hidratos_g"] == 62


def test_corregir_cambia_los_gramos_y_quita_el_rango(cli, con, lista):
    v = cli.post(f"/api/comidas/{lista}/revisar", json={"hidratos_g": 80, "grasa_g": 12}).json()
    assert v["corregida"] and v["hidratos_g"] == 80 and v["hidratos_min_g"] is None and v["grasa_g"] == 12
    _parametros(con, ratio=RATIO_FIJO)
    ahora = datetime.now(timezone.utc)
    _glucosa(con, ahora, 110)
    s = SG.sugerir(con, ahora=ahora, comida_id=lista, solicitud_lectura_id=_lectura_ok(con), dosis_apuntadas=True)
    assert float(s["hidratos_g"]) == 80 and float(s["u_hidratos"]) == 8.0
    assert s["incertidumbre"]["rango_u"] == [8.0, 8.0]           # sin rango: los gramos son de Daniel


def test_la_base_exige_fecha_de_revision_y_rango_coherente(con):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute("INSERT INTO comidas (ts, origen, revisada) VALUES (now(), 'manual', true)")
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute("""INSERT INTO comidas (ts, origen, rango_hidratos_min_g, rango_hidratos_max_g)
                       VALUES (now(), 'manual', 50, 40)""")


def test_el_trabajador_no_calcula_insulina():
    """Regla 1: el trabajador solo escribe nutrientes. Ni llama al bolo de ejemplo del prototipo
    ni toca tablas de dosis o sugerencias."""
    src = (pathlib.Path(T.__file__)).read_text()
    nombres = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    assert "calcular_bolo" not in nombres
    for tabla in ("INSERT INTO dosis", "sugerencias", "UPDATE dosis"):
        assert tabla not in src


# ------------------------------------------------------------------ rondas adaptativas
class TablaVacia:
    def buscar(self, nombre, nombre_en):
        return None, 0


def respuesta(hc):
    return {"items": [{"nombre": "plato", "nombre_en": "dish", "gramos": 300, "hidratos_g": hc, "fibra_g": 0,
                       "grasa_g": 10, "proteina_g": 20, "azucares_g": 0, "kcal": 400}]}


def consultas(valores):
    """Consulta simulada: cada modelo devuelve sus hidratos por turno."""
    hechas = []

    def consultar(modelo, prompt, b64):
        hechas.append(modelo)
        v = valores[modelo]
        return respuesta(v.pop(0) if isinstance(v, list) else v)
    consultar.hechas = hechas
    return consultar


def test_una_ronda_si_los_modelos_coinciden(monkeypatch):
    monkeypatch.setattr(T.C, "MODELOS", ["a", "b", "c"])
    monkeypatch.setattr(T.C, "MODELOS_TEXTO", ["a", "b", "c"])
    q = consultas({"a": 60, "b": 62, "c": 58})
    r = T.analizar_adaptativo(TablaVacia(), imagen=JPEG, consultar=q)
    assert q.hechas == ["a", "b", "c"] and r["rondas"] == 1 and r["hc_bolo"] == 60
    assert not any("repitió" in a for a in r["alertas"])


def test_segunda_ronda_si_discrepan_empezando_por_el_ultimo(monkeypatch):
    monkeypatch.setattr(T.C, "MODELOS", ["a", "b", "c"])
    monkeypatch.setattr(T.C, "MODELOS_TEXTO", ["a", "b", "c"])
    q = consultas({"a": [60, 61], "b": [62, 60], "c": [100, 59]})
    r = T.analizar_adaptativo(TablaVacia(), texto="paella", consultar=q)
    assert q.hechas == ["a", "b", "c", "c", "b", "a"] and r["rondas"] == 2
    assert "repitió" in r["alertas"][0]
    assert T.resumen(r)["estimaciones"]["rondas"] == 2


def test_nunca_mas_de_las_rondas_maximas(monkeypatch):
    monkeypatch.setattr(T.C, "MODELOS", ["a", "b"])
    monkeypatch.setattr(T.C, "MODELOS_TEXTO", ["a", "b"])
    monkeypatch.setattr(T, "RONDAS_MAX", 2)
    q = consultas({"a": 20, "b": 90})
    r = T.analizar_adaptativo(TablaVacia(), texto="x", consultar=q)
    assert len(q.hechas) == 4 and r["rondas"] == 2
    assert any("difieren mucho" in a for a in r["alertas"])     # la discrepancia sigue avisada


def test_un_modelo_caido_no_para_el_analisis(monkeypatch):
    monkeypatch.setattr(T.C, "MODELOS", ["a", "b"])
    monkeypatch.setattr(T.C, "MODELOS_TEXTO", ["a", "b"])

    def consultar(modelo, prompt, b64):
        if modelo == "a":
            raise TimeoutError("300 s")
        return respuesta(50)
    r = T.analizar_adaptativo(TablaVacia(), texto="x", consultar=consultar)
    assert r["hc_bolo"] == 50 and r["errores"] == ["a: 300 s"]


def test_la_consulta_acota_el_contexto(monkeypatch):
    enviado = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": '{"items": []}'}, "load_duration": 2e9, "eval_count": 10}

    def post(url, timeout, json):
        enviado.update(json)
        return Resp()
    monkeypatch.setattr(T.requests, "post", post)
    assert T.consultar_ollama("qwen3-vl:32b", "hola", "b64") == {"items": []}
    assert enviado["options"]["num_ctx"] == T.CONTEXTO == 8192 and enviado["keep_alive"]


# ------------------------------------------------------------------ la descripción guía a la foto
def test_la_descripcion_manda_en_el_prompt_de_la_foto(monkeypatch):
    monkeypatch.setattr(T.C, "MODELOS", ["a"])
    prompts = []

    def consultar(modelo, prompt, b64):
        prompts.append(prompt)
        return respuesta(5)
    T.analizar_adaptativo(TablaVacia(), imagen=JPEG, nota="steak tartar", consultar=consultar)
    assert "«steak tartar»" in prompts[0] and "Fíate de esa descripción" in prompts[0]
    T.analizar_adaptativo(TablaVacia(), imagen=JPEG, nota="", consultar=consultar)
    assert "Fíate" not in prompts[1]


def test_sin_base_usda_un_solo_aviso_claro():
    a = ["Valor atípico: x", "Sin coincidencia en la base (se usa el valor del modelo): steak tartar, tartar de carne"]
    assert T.limpiar_alertas(a, TablaVacia()) == ["Valor atípico: x", "Valores de los modelos: aún no hay base de alimentos USDA para contrastarlos."]

    class ConUSDA:
        usda = [1]
    assert T.limpiar_alertas(a, ConUSDA()) == a


def test_la_foto_guarda_la_descripcion_larga(cli, con):
    r = _foto(cli, id_cliente="foto-cliente-9", nota="steak tartar " * 50)
    assert r.status_code == 200 and con.execute("SELECT descripcion FROM comidas").fetchone()["descripcion"].startswith("steak")
