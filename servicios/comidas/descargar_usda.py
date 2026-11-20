"""Descarga la base USDA (SR Legacy, dominio público) y la convierte en SQLite con preparar_bd.py.

  docker compose --profile herramientas run --rm preparar-usda      # deja /datos/alimentos/alimentos.sqlite
  docker compose restart comidas                                     # el trabajador la carga al arrancar

Solo baja datos públicos de alimentos; no envía nada. Variables: USDA_URL (otra versión del zip) y
RUTA_ALIMENTOS_USDA (destino).
"""
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))
import config as C  # noqa: E402
import preparar_bd  # noqa: E402

URL = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip"
MAX_ZIP = 200 * 1024 * 1024


def descargar(url: str) -> bytes:
    r = requests.get(url, timeout=300, stream=True)
    r.raise_for_status()
    datos = io.BytesIO()
    for trozo in r.iter_content(1 << 20):
        datos.write(trozo)
        if datos.tell() > MAX_ZIP:
            raise RuntimeError("La descarga es demasiado grande")
    return datos.getvalue()


def preparar(zip_bytes: bytes, destino: Path) -> int:
    """Extrae solo los CSV que hacen falta y crea la base en `destino` (primero en un temporal)."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for nombre in z.namelist():
            base = Path(nombre).name
            if base in ("food.csv", "food_nutrient.csv") and ".." not in Path(nombre).parts:
                (Path(tmp) / base).write_bytes(z.read(nombre))
        nueva = destino.with_suffix(".nueva")
        C.RUTA_BD = str(nueva)
        preparar_bd.main(tmp)
        os.replace(nueva, destino)
    import sqlite3
    with sqlite3.connect(destino) as con:
        return con.execute("SELECT count(*) FROM alimentos").fetchone()[0]


def main() -> int:
    destino = Path(os.environ.get("RUTA_ALIMENTOS_USDA") or "/datos/alimentos/alimentos.sqlite")
    url = os.environ.get("USDA_URL") or URL
    print(f"Descargando {url}…")
    n = preparar(descargar(url), destino)
    print(f"Listo: {n} alimentos en {destino}. Reinicia el servicio comidas para usarla.")
    return 0 if n > 1000 else 1


if __name__ == "__main__":
    sys.exit(main())
