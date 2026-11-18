"""Núcleo: estima nutrientes de un plato (foto o descripción) con varios modelos locales
y una base de alimentos, y calcula un consenso con alertas de discrepancia.

Uso por línea de comandos:
  python nutricion.py --foto plato1.jpg --foto plato2.jpg [--nota "arroz integral"]
  python nutricion.py --texto "menú del día: lentejas con chorizo, filete con patatas fritas, flan"
"""
import argparse
import base64
import csv
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

import requests
from rapidfuzz import fuzz, process

import config as C

NUTS = ["hidratos", "fibra", "azucares", "grasa", "proteina", "kcal"]
CLAVE_MODELO = {"hidratos": "hidratos_g", "fibra": "fibra_g", "azucares": "azucares_g",
                "grasa": "grasa_g", "proteina": "proteina_g", "kcal": "kcal"}

ESQUEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "nombre_en": {"type": "string"},
                    "gramos": {"type": "number"},
                    "hidratos_g": {"type": "number"},
                    "fibra_g": {"type": "number"},
                    "azucares_g": {"type": "number"},
                    "grasa_g": {"type": "number"},
                    "proteina_g": {"type": "number"},
                    "kcal": {"type": "number"},
                },
                "required": ["nombre", "nombre_en", "gramos", "hidratos_g", "fibra_g",
                             "azucares_g", "grasa_g", "proteina_g", "kcal"],
            },
        }
    },
    "required": ["items"],
}

INSTRUCCIONES = """Eres un dietista experto en recuento de hidratos para diabetes tipo 1.
{fuente}
Separa cada alimento y estima su peso comestible en gramos tal como se va a comer
(cocinado si está cocinado; sin huesos ni cáscaras). Para cada alimento da:
- nombre: en español.
- nombre_en: en inglés al estilo de la base USDA, con el estado de cocción,
  p. ej. "Rice, white, long-grain, cooked", "Bread, wheat", "Potatoes, french fried".
- gramos.
- hidratos_g (totales, incluida la fibra), fibra_g, azucares_g, grasa_g, proteina_g y kcal
  para esa cantidad, según tu propio conocimiento.
No inventes alimentos que no aparezcan. Incluye salsas, aceite visible, pan y bebidas si aparecen.
Responde solo con el JSON pedido."""

FUENTE_FOTO = "Analiza la foto del plato.{nota}"
FUENTE_TEXTO = ("No hay foto. Analiza esta descripción de una comida. Si no se indica cantidad, "
                "usa raciones típicas de restaurante en España.\nDescripción: {texto}")


# ---------------------------------------------------------------- base de alimentos
class TablaAlimentos:
    def __init__(self, ruta_bd=C.RUTA_BD, ruta_propios=C.RUTA_PROPIOS):
        self.usda, self.propios = [], []
        if Path(ruta_bd).exists():
            con = sqlite3.connect(ruta_bd)
            con.row_factory = sqlite3.Row
            self.usda = [dict(r) | {"fuente": "USDA"} for r in con.execute("SELECT * FROM alimentos")]
            con.close()
        if Path(ruta_propios).exists():
            with open(ruta_propios, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    fila = {k: float(r[k] or 0) for k in NUTS}
                    # En el CSV propio los hidratos van como en etiqueta europea (sin fibra):
                    # se suma la fibra para guardar hidratos totales, igual que USDA.
                    fila["hidratos"] += fila["fibra"]
                    fila["nombre"] = r["nombre"]
                    fila["fuente"] = "propio"
                    nombres = [r["nombre"]] + [a for a in (r.get("alias") or "").split("|") if a]
                    for n in nombres:
                        self.propios.append((n.lower(), fila))
        self._nombres_usda = [a["nombre"] for a in self.usda]

    def buscar(self, nombre_es: str, nombre_en: str):
        """Devuelve (fila por 100 g, puntuación) o (None, 0)."""
        if self.propios and nombre_es:
            m = process.extractOne(nombre_es.lower(), [p[0] for p in self.propios], scorer=fuzz.WRatio)
            if m and m[1] >= max(C.UMBRAL_COINCIDENCIA, 88):
                return self.propios[m[2]][1], m[1]
        if self._nombres_usda and nombre_en:
            m = process.extractOne(nombre_en, self._nombres_usda, scorer=fuzz.WRatio)
            if m and m[1] >= C.UMBRAL_COINCIDENCIA:
                return self.usda[m[2]], m[1]
        return None, 0


# ---------------------------------------------------------------- modelos
def consultar(modelo: str, prompt: str, imagen_b64: str | None = None) -> dict:
    msg = {"role": "user", "content": prompt}
    if imagen_b64:
        msg["images"] = [imagen_b64]
    r = requests.post(f"{C.OLLAMA_URL}/api/chat", timeout=C.TIMEOUT, json={
        "model": modelo, "messages": [msg], "format": ESQUEMA, "stream": False,
        "options": {"temperature": C.TEMPERATURA}})
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


def procesar_respuesta(resp: dict, tabla: TablaAlimentos) -> dict:
    """Convierte una respuesta de modelo en items + totales por las dos vías."""
    items = []
    for it in resp.get("items", []):
        g = max(0.0, float(it.get("gramos", 0) or 0))
        est_modelo = {k: max(0.0, float(it.get(CLAVE_MODELO[k], 0) or 0)) for k in NUTS}
        fila, punt = tabla.buscar(it.get("nombre", ""), it.get("nombre_en", ""))
        if fila:
            por100 = {k: fila[k] for k in NUTS}
            fuente = f"{fila['fuente']}: {fila['nombre']} ({punt:.0f})"
        else:  # sin coincidencia: se usa la estimación del modelo escalada
            por100 = {k: (est_modelo[k] / g * 100 if g else 0.0) for k in NUTS}
            fuente = "modelo (sin coincidencia en BD)"
        items.append({"nombre": it.get("nombre", ""), "nombre_en": it.get("nombre_en", ""),
                      "gramos": g, "por100": por100, "fuente": fuente,
                      "en_bd": fila is not None, "modelo": est_modelo})
    tot_bd = {k: sum(i["por100"][k] * i["gramos"] / 100 for i in items) for k in NUTS}
    tot_mod = {k: sum(i["modelo"][k] for i in items) for k in NUTS}
    return {"items": items, "via_bd": tot_bd, "via_modelo": tot_mod}


def hc_bolo(t: dict) -> float:
    return max(0.0, t["hidratos"] - (t["fibra"] if C.RESTAR_FIBRA else 0.0))


def mediana_dict(lista):
    return {k: statistics.median(d[k] for d in lista) for k in NUTS}


def analizar_plato(tabla: TablaAlimentos, imagen: bytes | None = None, nota: str = "",
                   texto: str | None = None, progreso=print) -> dict:
    if imagen is not None:
        b64 = base64.b64encode(imagen).decode()
        prompt = INSTRUCCIONES.format(fuente=FUENTE_FOTO.format(nota=f"\nNota del usuario: {nota}" if nota else ""))
        modelos = C.MODELOS
    else:
        b64 = None
        prompt = INSTRUCCIONES.format(fuente=FUENTE_TEXTO.format(texto=texto))
        modelos = C.MODELOS_TEXTO

    corridas, errores = [], []
    for m in modelos:
        for rep in range(C.REPETICIONES):
            progreso(f"  {m} ({rep + 1}/{C.REPETICIONES})…")
            try:
                r = procesar_respuesta(consultar(m, prompt, b64), tabla)
                r["modelo_id"] = m
                corridas.append(r)
            except Exception as e:  # noqa: BLE001
                errores.append(f"{m}: {e}")
    if not corridas:
        raise RuntimeError("Ningún modelo respondió:\n" + "\n".join(errores))
    return consolidar(corridas, errores, origen="foto" if imagen is not None else "texto")


def consolidar(corridas, errores, origen) -> dict:
    alertas = []
    por_modelo = {}
    for m in dict.fromkeys(c["modelo_id"] for c in corridas):
        cs = [c for c in corridas if c["modelo_id"] == m]
        bd, mod = mediana_dict([c["via_bd"] for c in cs]), mediana_dict([c["via_modelo"] for c in cs])
        hcs = [hc_bolo(c["via_bd"]) for c in cs]
        cv = (statistics.pstdev(hcs) / statistics.mean(hcs)) if len(hcs) > 1 and statistics.mean(hcs) > 0 else 0
        por_modelo[m] = {"via_bd": bd, "via_modelo": mod, "cv": cv, "n": len(cs)}
        if cv > C.ALERTA_CV_MODELO:
            alertas.append(f"{m} es inestable consigo mismo (variación {cv:.0%}).")

    estimaciones = []  # (etiqueta, totales)
    for m, d in por_modelo.items():
        estimaciones.append((f"{m} · BD", d["via_bd"]))
        estimaciones.append((f"{m} · modelo", d["via_modelo"]))
    consenso = mediana_dict([e[1] for e in estimaciones])
    hc = [hc_bolo(e[1]) for e in estimaciones]
    hc_med = hc_bolo(consenso)
    dispersion = max(hc) - min(hc)
    if dispersion > max(C.ALERTA_ABS_G, C.ALERTA_REL * hc_med):
        alertas.append(f"Las estimaciones de hidratos difieren mucho: de {min(hc):.0f} a {max(hc):.0f} g.")
    for etiqueta, t in estimaciones:
        if abs(hc_bolo(t) - hc_med) > max(C.ALERTA_ABS_G, C.ALERTA_REL * hc_med):
            alertas.append(f"Valor atípico: {etiqueta} = {hc_bolo(t):.0f} g (consenso {hc_med:.0f} g).")

    sin_bd = {i["nombre"] for c in corridas for i in c["items"] if not i["en_bd"]}
    if sin_bd:
        alertas.append("Sin coincidencia en la base (se usa el valor del modelo): " + ", ".join(sorted(sin_bd)))

    # Corrida representativa (la más cercana al consenso) para la tabla editable
    rep = min(corridas, key=lambda c: abs(hc_bolo(c["via_bd"]) - hc_med))
    return {"origen": origen, "consenso": consenso, "hc_bolo": hc_med, "rango_hc": (min(hc), max(hc)),
            "estimaciones": [(e, {k: round(v, 1) for k, v in t.items()}) for e, t in estimaciones],
            "items": rep["items"], "alertas": alertas, "errores": errores,
            "modelos_items": {c["modelo_id"]: [i["nombre"] for i in c["items"]] for c in corridas}}


# ---------------------------------------------------------------- comida completa y bolo
def totales_items(items) -> dict:
    return {k: sum(i["por100"][k] * i["gramos"] / 100 for i in items) for k in NUTS}


def calcular_bolo(t: dict, ratio=C.RATIO_HC, sens=C.SENSIBILIDAD, objetivo=C.OBJETIVO,
                  glucosa=None, insulina_activa=0.0) -> dict:
    """Bolo de referencia (solo para pruebas). Incluye método de Varsovia (UGP)."""
    def red(x):
        return round(x / C.REDONDEO_U) * C.REDONDEO_U
    hc = hc_bolo(t)
    u_hc = hc / ratio
    u_corr = ((glucosa - objetivo) / sens) if glucosa else 0.0
    inmediato = max(0.0, u_hc + u_corr - (insulina_activa or 0))
    ugp = (t["grasa"] * 9 + t["proteina"] * 4) / 100
    u_ext = ugp * 10 / ratio
    horas = 0 if ugp < 1 else 3 if ugp < 2 else 4 if ugp < 3 else 5 if ugp < 4 else 8
    return {"hc_bolo": hc, "u_hidratos": u_hc, "u_correccion": u_corr,
            "bolo_inmediato": red(inmediato), "ugp": ugp,
            "bolo_extendido": red(u_ext) if ugp >= 1 else 0.0, "horas_extendido": horas}


def guardar_registro(platos, items_finales, bolo, ruta=C.RUTA_REGISTRO):
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(json.dumps({"fecha": datetime.now().isoformat(timespec="seconds"),
                            "platos": platos, "items_finales": items_finales, "bolo": bolo},
                           ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- CLI
def imprimir_plato(nombre, r):
    print(f"\n=== {nombre} ({r['origen']}) ===")
    c = r["consenso"]
    print(f"Hidratos para bolo: {r['hc_bolo']:.0f} g  (rango {r['rango_hc'][0]:.0f}–{r['rango_hc'][1]:.0f} g)")
    print(f"Hidratos totales {c['hidratos']:.0f} g · fibra {c['fibra']:.0f} g · azúcares {c['azucares']:.0f} g · "
          f"grasa {c['grasa']:.0f} g · proteína {c['proteina']:.0f} g · {c['kcal']:.0f} kcal")
    print("Estimaciones:")
    for e, t in r["estimaciones"]:
        print(f"  {e:<32} {hc_bolo(t):6.1f} g")
    print("Alimentos (corrida representativa):")
    for i in r["items"]:
        print(f"  {i['nombre']:<28} {i['gramos']:6.0f} g   [{i['fuente']}]")
    for a in r["alertas"]:
        print(f"  ⚠ {a}")
    for e in r["errores"]:
        print(f"  ✗ {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--foto", action="append", default=[], help="foto de un plato (repetible)")
    ap.add_argument("--nota", default="", help="nota opcional para las fotos")
    ap.add_argument("--texto", help="descripción de la comida (sin foto)")
    ap.add_argument("--glucosa", type=float, help="glucosa actual (mg/dL)")
    ap.add_argument("--activa", type=float, default=0.0, help="insulina activa (U)")
    a = ap.parse_args()
    if not a.foto and not a.texto:
        ap.error("indica --foto o --texto")

    tabla = TablaAlimentos()
    if not tabla.usda:
        print("Aviso: no hay base USDA (ejecuta preparar_bd.py); se usarán solo alimentos propios y modelos.")
    platos = []
    for ruta in a.foto:
        print(f"Analizando {ruta}…")
        r = analizar_plato(tabla, imagen=Path(ruta).read_bytes(), nota=a.nota)
        imprimir_plato(ruta, r)
        platos.append(r)
    if a.texto:
        print("Analizando descripción…")
        r = analizar_plato(tabla, texto=a.texto)
        imprimir_plato("Descripción", r)
        platos.append(r)

    total = {k: sum(p["consenso"][k] for p in platos) for k in NUTS}
    b = calcular_bolo(total, glucosa=a.glucosa, insulina_activa=a.activa)
    print("\n=== COMIDA COMPLETA (consenso) ===")
    print(f"Hidratos para bolo {b['hc_bolo']:.0f} g · grasa {total['grasa']:.0f} g · proteína {total['proteina']:.0f} g · "
          f"fibra {total['fibra']:.0f} g · {total['kcal']:.0f} kcal")
    print(f"Bolo de referencia: {b['bolo_inmediato']} U inmediatas"
          + (f" + {b['bolo_extendido']} U extendidas en {b['horas_extendido']} h ({b['ugp']:.1f} UGP)" if b["bolo_extendido"] else ""))
    guardar_registro(platos, [i for p in platos for i in p["items"]], b)


if __name__ == "__main__":
    main()
