"""Preparación de la base USDA desde un zip sintético (sin red)."""
import io
import sqlite3
import zipfile

from servicios.comidas import descargar_usda as U


def _zip():
    food = "fdc_id,data_type,description\n1,sr_legacy_food,\"Rice, white, cooked\"\n2,sr_legacy_food,\"Beef, raw\"\n"
    nut = ("id,fdc_id,nutrient_id,amount\n1,1,1005,28.2\n2,1,1003,2.7\n3,1,1079,0.4\n"
           "4,2,1003,20.0\n5,2,1004,15.0\n")
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("FoodData_Central_sr_legacy/food.csv", food)
        z.writestr("FoodData_Central_sr_legacy/food_nutrient.csv", nut)
        z.writestr("../malo.csv", "x")
    return b.getvalue()


def test_prepara_la_base(tmp_path):
    destino = tmp_path / "alimentos" / "alimentos.sqlite"
    assert U.preparar(_zip(), destino) == 2
    with sqlite3.connect(destino) as con:
        arroz = con.execute("SELECT hidratos, fibra, proteina FROM alimentos WHERE id = '1'").fetchone()
    assert arroz == (28.2, 0.4, 2.7)
    assert not (tmp_path / "malo.csv").exists() and not destino.with_suffix(".nueva").exists()
