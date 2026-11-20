"""Bases de alimentos abiertas además de USDA: CIQUAL (Francia) y CoFID (Reino Unido).

  docker compose --profile herramientas run --rm preparar-alimentos      # descarga y prepara todas
  python -m servicios.comidas.fuentes_alimentos ciqual --carpeta DIR     # desde archivos ya bajados

Cada base se guarda en su propio SQLite con el mismo esquema que la de USDA (`alimentos`), con
nutrientes por 100 g y **hidratos totales con la fibra incluida**, igual que USDA:
  - CIQUAL da "Glucides" sin fibra (convenio europeo) → hidratos = glucides + fibres.
  - CoFID da "Carbohydrate" disponible sin fibra → hidratos = carbohydrate + AOAC fibre.
El trabajador resta la fibra para el bolo (config.RESTAR_FIBRA), así que el resultado es el mismo
convenio en todas. Se busca por el nombre en inglés que da el modelo (`nombre_en`).

Licencias (uso personal, citando la fuente):
  - CIQUAL 2025, ANSES, Licence Ouverte / Open Licence 2.0 (etalab-2.0). doi:10.57745/RDMHWY
  - CoFID 2021, McCance and Widdowson's, © Crown copyright, Open Government Licence v3.0.
Solo se descargan datos públicos; no se envía nada.
"""
import argparse
import io
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import requests

CARPETA = Path(os.environ.get("RUTA_ALIMENTOS") or "/datos/alimentos")
API_CIQUAL = "https://entrepot.recherche.data.gouv.fr/api/access/datafile/"
CIQUAL = {"alim": API_CIQUAL + "666252", "compo": API_CIQUAL + "666249"}
COFID = ("https://assets.publishing.service.gov.uk/media/60538b91e90e07527df82ae4/"
         "McCance_Widdowsons_Composition_of_Foods_Integrated_Dataset_2021..xlsx")
MAX_DESCARGA = 200 * 1024 * 1024
COLUMNAS = ("kcal", "hidratos", "fibra", "azucares", "grasa", "proteina")

# CIQUAL: const_code → columna
CONST_CIQUAL = {"328": "kcal", "31000": "glucides", "34100": "fibra", "32000": "azucares", "40000": "grasa",
                "25000": "proteina"}
# CoFID, hoja "1.3 Proximates": código de la fila 2 → columna
COD_COFID = {"KCALS": "kcal", "CHO": "glucides", "AOACFIB": "fibra", "TOTSUG": "azucares", "FAT": "grasa",
             "PROT": "proteina"}


# ------------------------------------------------------------------ utilidades
def numero(v) -> float | None:
    """'49,9' → 49.9 · 'traces', 'Tr', '<0,5' → 0 · '-', 'N', '' → None (sin dato)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    if s in ("", "-", "N", "n/a"):
        return None
    if s.lower() in ("traces", "tr", "trace") or s.startswith("<"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def fila(nombre: str, glucides, fibra, **otros) -> dict | None:
    """Fila por 100 g con hidratos totales (con fibra). Sin dato de hidratos no vale."""
    if not nombre or glucides is None:
        return None
    fib = fibra or 0.0
    d = {"nombre": nombre.strip(), "hidratos": glucides + fib, "fibra": fib}
    for k in ("azucares", "grasa", "proteina"):
        d[k] = otros.get(k) or 0.0
    d["kcal"] = otros.get("kcal")
    if d["kcal"] is None:
        d["kcal"] = 4 * glucides + 2 * fib + 4 * d["proteina"] + 9 * d["grasa"]
    if not (0 <= d["hidratos"] <= 100):
        return None
    return d


def guardar(filas: dict[str, dict], destino: Path) -> int:
    destino.parent.mkdir(parents=True, exist_ok=True)
    nueva = destino.with_suffix(".nueva")
    nueva.unlink(missing_ok=True)
    with sqlite3.connect(nueva) as con:
        con.execute("""CREATE TABLE alimentos (id TEXT PRIMARY KEY, nombre TEXT,
            kcal REAL, hidratos REAL, fibra REAL, azucares REAL, grasa REAL, proteina REAL)""")
        con.executemany("INSERT INTO alimentos VALUES (?,?,?,?,?,?,?,?)",
                        [(i, f["nombre"], *(f[c] for c in COLUMNAS)) for i, f in filas.items()])
    os.replace(nueva, destino)
    return len(filas)


def descargar(url: str) -> bytes:
    r = requests.get(url, timeout=600, stream=True)
    r.raise_for_status()
    datos = io.BytesIO()
    for trozo in r.iter_content(1 << 20):
        datos.write(trozo)
        if datos.tell() > MAX_DESCARGA:
            raise RuntimeError("La descarga es demasiado grande")
    return datos.getvalue()


# ------------------------------------------------------------------ CIQUAL (XML)
def _elementos(xml: bytes, etiqueta: str):
    for _, e in ET.iterparse(io.BytesIO(xml), events=("end",)):
        if e.tag == etiqueta:
            yield {h.tag: (h.text or "").strip() for h in e}
            e.clear()


def leer_ciqual(alim_xml: bytes, compo_xml: bytes) -> dict[str, dict]:
    nombres = {a["alim_code"]: a.get("alim_nom_eng") or a.get("alim_nom_fr") for a in _elementos(alim_xml, "ALIM")}
    valores: dict[str, dict] = {}
    for c in _elementos(compo_xml, "COMPO"):
        col = CONST_CIQUAL.get(c.get("const_code"))
        if col and c.get("alim_code") in nombres:
            valores.setdefault(c["alim_code"], {})[col] = numero(c.get("teneur"))
    out = {}
    for codigo, v in valores.items():
        f = fila(nombres[codigo], v.get("glucides"), v.get("fibra"),
                 **{k: v.get(k) for k in ("kcal", "azucares", "grasa", "proteina")})
        if f:
            out[f"ciqual-{codigo}"] = f
    return out


# ------------------------------------------------------------------ CoFID (xlsx, sin dependencias)
def _hoja_xlsx(xlsx: bytes, nombre_hoja: str) -> list[list]:
    """Filas de una hoja como listas de valores (texto o número). Solo lo necesario para CoFID."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    z = zipfile.ZipFile(io.BytesIO(xlsx))
    libro = ET.fromstring(z.read("xl/workbook.xml"))
    rid = next(h.get(f"{{{ns['r']}}}id") for h in libro.iter(f"{{{ns['m']}}}sheet") if h.get("name") == nombre_hoja)
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    destino = next(r.get("Target") for r in rels if r.get("Id") == rid).lstrip("/")
    ruta = destino if destino.startswith("xl/") else "xl/" + destino
    compartidas = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{{{ns['m']}}}si"):
            compartidas.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
    filas = []
    for r in ET.fromstring(z.read(ruta)).iter(f"{{{ns['m']}}}row"):
        fila_ = {}
        for c in r.iter(f"{{{ns['m']}}}c"):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            n = 0
            for ch in col:
                n = n * 26 + ord(ch) - 64
            v = c.find(f"{{{ns['m']}}}v")
            t = c.get("t")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            elif v is None:
                val = None
            elif t == "s":
                val = compartidas[int(v.text)]
            elif t in ("str", "b", "e"):
                val = v.text
            else:
                val = float(v.text)
            fila_[n - 1] = val
        filas.append([fila_.get(i) for i in range(max(fila_) + 1)] if fila_ else [])
    return filas


def leer_cofid(xlsx: bytes) -> dict[str, dict]:
    filas = _hoja_xlsx(xlsx, "1.3 Proximates")
    codigos = [str(x or "").strip() for x in filas[1]]                 # fila 2: WATER, PROT, CHO…
    i_cod, i_nom = 0, 1                                                # Food Code, Food Name
    idx = {col: codigos.index(cod) for cod, col in COD_COFID.items() if cod in codigos}
    if "glucides" not in idx:
        raise RuntimeError("CoFID: no encuentro la columna CHO en «1.3 Proximates»")
    out = {}
    for r in filas[3:]:                                                # los datos empiezan en la fila 4
        if len(r) <= i_nom or not r[i_cod] or not r[i_nom]:
            continue
        v = {col: numero(r[i]) if i < len(r) else None for col, i in idx.items()}
        f = fila(str(r[i_nom]), v.get("glucides"), v.get("fibra"),
                 **{k: v.get(k) for k in ("kcal", "azucares", "grasa", "proteina")})
        if f:
            out[f"cofid-{str(r[i_cod]).strip()}"] = f
    return out


# ------------------------------------------------------------------ línea de órdenes
def preparar(fuente: str, carpeta_local: Path | None = None) -> int:
    def leer(nombre: str, url: str) -> bytes:
        if carpeta_local is not None:
            return (carpeta_local / nombre).read_bytes()
        print(f"Descargando {url}…")
        return descargar(url)
    if fuente == "ciqual":
        filas = leer_ciqual(leer("ciqual_alim.xml", CIQUAL["alim"]), leer("ciqual_compo.xml", CIQUAL["compo"]))
    elif fuente == "cofid":
        filas = leer_cofid(leer("cofid.xlsx", COFID))
    else:
        raise ValueError(fuente)
    n = guardar(filas, CARPETA / f"{fuente}.sqlite")
    print(f"{fuente}: {n} alimentos en {CARPETA / (fuente + '.sqlite')}")
    return n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("fuente", nargs="?", default="todas", choices=["todas", "ciqual", "cofid"])
    p.add_argument("--carpeta", type=Path, help="usar archivos ya descargados (ciqual_alim.xml, ciqual_compo.xml, cofid.xlsx)")
    a = p.parse_args(argv)
    for f in (["ciqual", "cofid"] if a.fuente == "todas" else [a.fuente]):
        preparar(f, a.carpeta)
    print("Reinicia el trabajador: docker compose restart comidas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
