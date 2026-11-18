"""Convierte la descarga CSV de USDA FoodData Central (SR Legacy) en una base SQLite local.

Uso:
  1. Descarga "SR Legacy" en CSV desde https://fdc.nal.usda.gov/download-datasets
  2. Descomprime y ejecuta:  python preparar_bd.py ruta/a/la/carpeta_descomprimida
"""
import csv
import sqlite3
import sys
from pathlib import Path

import config as C

# id de nutriente en FoodData Central -> columna (valores por 100 g)
NUTRIENTES = {
    1008: "kcal",
    1003: "proteina",
    1004: "grasa",
    1005: "hidratos",   # por diferencia, incluye fibra
    1079: "fibra",
    2000: "azucares",
}


def buscar(carpeta: Path, nombre: str) -> Path:
    encontrados = list(carpeta.rglob(nombre))
    if not encontrados:
        sys.exit(f"No encuentro {nombre} dentro de {carpeta}")
    return encontrados[0]


def main(carpeta: str):
    carpeta = Path(carpeta)
    csv.field_size_limit(10_000_000)

    alimentos = {}
    with open(buscar(carpeta, "food.csv"), encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            alimentos[fila["fdc_id"]] = {"nombre": fila["description"]}

    with open(buscar(carpeta, "food_nutrient.csv"), encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            try:
                nid = int(fila["nutrient_id"])
            except (KeyError, ValueError):
                continue
            col = NUTRIENTES.get(nid)
            if col and fila["fdc_id"] in alimentos and fila["amount"]:
                alimentos[fila["fdc_id"]][col] = float(fila["amount"])

    con = sqlite3.connect(C.RUTA_BD)
    con.execute("DROP TABLE IF EXISTS alimentos")
    con.execute("""CREATE TABLE alimentos (id TEXT PRIMARY KEY, nombre TEXT,
        kcal REAL, hidratos REAL, fibra REAL, azucares REAL, grasa REAL, proteina REAL)""")
    n = 0
    for fid, a in alimentos.items():
        if "hidratos" not in a and "proteina" not in a:
            continue
        for k in ("hidratos", "fibra", "azucares", "grasa", "proteina"):
            a.setdefault(k, 0.0)
        if "kcal" not in a:
            a["kcal"] = 4 * a["hidratos"] + 4 * a["proteina"] + 9 * a["grasa"]
        con.execute("INSERT INTO alimentos VALUES (?,?,?,?,?,?,?,?)",
                    (fid, a["nombre"], a["kcal"], a["hidratos"], a["fibra"],
                     a["azucares"], a["grasa"], a["proteina"]))
        n += 1
    con.commit()
    con.close()
    print(f"Base creada en {C.RUTA_BD} con {n} alimentos.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
