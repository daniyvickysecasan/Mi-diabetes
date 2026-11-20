"""Bases de alimentos abiertas (CIQUAL, CoFID) y búsqueda en varias bases. Datos sintéticos."""
import io
import sqlite3
import zipfile

import pytest

from servicios.comidas import fuentes_alimentos as F
from servicios.comidas import trabajador as T

ALIM = """﻿<?xml version="1.0" encoding="utf-8" ?>
<TABLE>
   <ALIM><alim_code> 9100 </alim_code><alim_nom_fr> Riz blanc, cuit </alim_nom_fr>
      <alim_nom_eng> Rice, white, cooked </alim_nom_eng><alim_nom_sci missing=" " /></ALIM>
   <ALIM><alim_code> 9200 </alim_code><alim_nom_fr> Sel </alim_nom_fr><alim_nom_eng> Salt </alim_nom_eng></ALIM>
   <ALIM><alim_code> 9300 </alim_code><alim_nom_fr> Pomme </alim_nom_fr><alim_nom_eng missing=" " /></ALIM>
</TABLE>""".encode("utf-8")


def compo(*filas):
    cuerpo = "".join(f"<COMPO><alim_code> {a} </alim_code><const_code> {c} </const_code><teneur> {t} </teneur>"
                     f"<min missing=\" \" /></COMPO>" for a, c, t in filas)
    return f"﻿<?xml version=\"1.0\" encoding=\"utf-8\" ?><TABLE>{cuerpo}</TABLE>".encode("utf-8")


@pytest.mark.parametrize("v, sale", [("49,9", 49.9), (" 12 ", 12.0), ("traces", 0.0), ("Tr", 0.0), ("<0,5", 0.0),
                                     ("-", None), ("N", None), ("", None), (None, None), (3, 3.0), ("abc", None)])
def test_numero(v, sale):
    assert F.numero(v) == sale


def test_ciqual_suma_la_fibra_y_descarta_sin_hidratos():
    filas = F.leer_ciqual(ALIM, compo(
        (9100, 31000, "31,2"), (9100, 34100, "1,4"), (9100, 328, "155"), (9100, 40000, "0,7"), (9100, 25000, "3,1"),
        (9100, 32000, "traces"), (9100, 10110, "5"),                  # sodio: no interesa
        (9200, 31000, "-"),                                          # sin dato de hidratos: fuera
        (9300, 31000, "11,6"), (9300, 34100, "-")))                  # sin fibra: 0; sin nombre inglés: el francés
    assert set(filas) == {"ciqual-9100", "ciqual-9300"}
    r = filas["ciqual-9100"]
    assert r["nombre"] == "Rice, white, cooked" and r["hidratos"] == pytest.approx(32.6) and r["fibra"] == 1.4
    assert (r["kcal"], r["grasa"], r["proteina"], r["azucares"]) == (155, 0.7, 3.1, 0.0)
    assert filas["ciqual-9300"]["nombre"] == "Pomme" and filas["ciqual-9300"]["hidratos"] == 11.6


def _xlsx(filas: list[list]) -> bytes:
    """xlsx mínimo con la hoja «1.3 Proximates» (texto en línea y números)."""
    def celda(i, j, v):
        ref = chr(65 + j) + str(i + 1)
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    hoja = "".join(f'<row r="{i + 1}">' + "".join(celda(i, j, v) for j, v in enumerate(f)) + "</row>"
                   for i, f in enumerate(filas))
    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{m}" xmlns:r="{r}"><sheets>'
                   f'<sheet name="Notas" sheetId="1" r:id="rId1"/><sheet name="1.3 Proximates" sheetId="2" r:id="rId2"/>'
                   "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{m}"><sheetData/></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{m}"><sheetData>{hoja}</sheetData></worksheet>')
    return b.getvalue()


def test_cofid_lee_la_hoja_de_proximales():
    x = _xlsx([
        ["Food Code", "Food Name", "Description", "Protein (g)", "Fat (g)", "Carbohydrate (g)", "Energy (kcal) (kcal)",
         "Total sugars (g)", "AOAC fibre (g)"],
        ["", "", "", "PROT", "FAT", "CHO", "KCALS", "TOTSUG", "AOACFIB"],
        ["", "", "", "Protein, total", "Fat, total", "Carbohydrate, available", "Energy", "Sugars", "Fibre"],
        ["13-145", "Ackee, canned, drained", "x", 2.9, 15.2, 0.8, 151, "Tr", "N"],
        ["11-1", "Bread, white", "x", 7.9, 1.6, 46.1, 238, 2.6, 2.5],
        ["11-2", "Algo sin hidratos", "x", 1, 1, "N", 10, 0, 0],
    ])
    filas = F.leer_cofid(x)
    assert set(filas) == {"cofid-13-145", "cofid-11-1"}
    assert filas["cofid-11-1"]["hidratos"] == pytest.approx(48.6) and filas["cofid-11-1"]["fibra"] == 2.5
    assert filas["cofid-13-145"]["hidratos"] == 0.8 and filas["cofid-13-145"]["azucares"] == 0.0


def test_preparar_desde_carpeta_local(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "CARPETA", tmp_path / "salida")
    (tmp_path / "ciqual_alim.xml").write_bytes(ALIM)
    (tmp_path / "ciqual_compo.xml").write_bytes(compo((9100, 31000, "30")))
    assert F.preparar("ciqual", tmp_path) == 1
    with sqlite3.connect(tmp_path / "salida" / "ciqual.sqlite") as con:
        assert con.execute("SELECT nombre, hidratos FROM alimentos").fetchall() == [("Rice, white, cooked", 30.0)]
    assert not (tmp_path / "salida" / "ciqual.nueva").exists()


# ------------------------------------------------------------------ búsqueda en varias bases
def _base(ruta, filas):
    with sqlite3.connect(ruta) as con:
        con.execute("""CREATE TABLE alimentos (id TEXT PRIMARY KEY, nombre TEXT,
            kcal REAL, hidratos REAL, fibra REAL, azucares REAL, grasa REAL, proteina REAL)""")
        con.executemany("INSERT INTO alimentos VALUES (?,?,0,?,0,0,0,0)", filas)


@pytest.fixture
def tabla(tmp_path):
    _base(tmp_path / "alimentos.sqlite", [("u1", "Rice, white, long-grain, regular, cooked", 28.2),
                                          ("u2", "Kebab, beef, cooked", 5.0)])
    _base(tmp_path / "ciqual.sqlite", [("c1", "Rice, white, cooked, no added salt", 34.6)])
    _base(tmp_path / "cofid.sqlite", [("f1", "Rice, white, basmati, boiled in unsalted water", 27.1)])
    propios = tmp_path / "propios.csv"
    propios.write_text("nombre,alias,kcal,hidratos,fibra,azucares,grasa,proteina\n"
                       "pan de barra,baguette,277,56.0,3.5,2.6,1.6,8.5\n", encoding="utf-8")
    return T.TablaVarias(tmp_path / "alimentos.sqlite", propios, tmp_path)


def test_orden_propios_luego_mejor_coincidencia_con_preferencia_europea(tabla):
    assert tabla.resumen() == "CIQUAL 1, CoFID 1, USDA 2"
    f, _ = tabla.buscar("pan de barra", "French bread")
    assert f["fuente"] == "propio"
    f, _ = tabla.buscar("arroz blanco cocido", "white rice cooked")
    assert f["fuente"] == "CIQUAL" and f["hidratos"] == 34.6
    f, _ = tabla.buscar("kebab", "beef kebab")                     # solo está en USDA
    assert f["fuente"] == "USDA"
    assert tabla.buscar("algo", "zzzz qqqq") == (None, 0)
    assert tabla.buscar("algo", "") == (None, 0)


def test_sin_bases_extra_sigue_como_antes(tmp_path):
    t = T.TablaVarias(tmp_path / "no-existe.sqlite", tmp_path / "no.csv", tmp_path)
    assert t.bases == [] and t.resumen() == "ninguna" and t.buscar("arroz", "rice") == (None, 0)


# ------------------------------------------------------------------ coincidencia en inglés
from servicios.comidas import coincidencia as K  # noqa: E402

NOMBRES = ["Egg white, cooked", "Kir (white wine with blackcurrant liqueur)", "Rice, white, cooked, no added salt",
           "Apple, dried", "Apple, flesh and skin, raw", "Chutney, apple, homemade", "Coconut milk",
           "Milk, whole (average)", "Milk bread, homemade", "Lentils, boiled"]


@pytest.mark.parametrize("consulta, esperado", [
    ("cooked white rice", "Rice, white, cooked, no added salt"),   # antes: "Egg white, cooked" o el kir
    ("white rice cooked", "Rice, white, cooked, no added salt"),
    ("apple", "Apple, flesh and skin, raw"),                      # no la deshidratada ni el chutney
    ("apples", "Apple, flesh and skin, raw"),                     # plural
    ("milk", "Milk, whole (average)"),                            # no la de coco ni el pan
    ("lentil", "Lentils, boiled"),
    ("paella", None),                                             # sin coincidencia: valor del modelo
    ("kebab", None),
])
def test_coincidencia_exige_todas_las_palabras_y_penaliza_las_que_transforman(consulta, esperado):
    m = K.Indice(NOMBRES).mejor(consulta)
    assert (NOMBRES[m[0]] if m else None) == esperado


def test_solo_lo_transformado_no_llega_al_umbral():
    assert K.Indice(["Apple, dried"]).mejor("apple") is None
    assert K.Indice(["Apple, dried"]).mejor("dried apple") is not None
    assert K.raiz("tomatoes") == "tomato" and K.raiz("berries") == "berry" and K.raiz("glass") == "glass"
