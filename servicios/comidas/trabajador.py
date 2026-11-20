"""Trabajador de comidas: analiza con los modelos locales (Ollama) las comidas que deja la API.

  python -m servicios.comidas.trabajador servicio      # bucle: mira la base cada pocos segundos
  python -m servicios.comidas.trabajador una ID        # analiza una comida ya (pruebas)

Usa el núcleo del prototipo (nutricion.py: consulta, dos vías, mediana y alertas) sin cambiarlo,
pero con repeticiones adaptativas (Daniel, 19-11-2026): una consulta por modelo y, solo si las
estimaciones de hidratos discrepan mucho, otra ronda para ver si cambian. Solo se comunica con la API por la tabla `comidas` (regla 3 de CLAUDE.md).

REGLA 1: lo que escribe aquí son ESTIMACIONES de nutrientes, nunca unidades de insulina. Esos gramos
no entran en ningún cálculo de dosis hasta que Daniel revisa la comida en la app.

Variables de entorno:
  OLLAMA_URL              http://host.docker.internal:11434 en Docker
  COMIDAS_MODELOS         lista separada por comas (por defecto, la de config.py)
  COMIDAS_RONDAS_MAX      rondas como máximo (por defecto 2: la segunda solo si hay discrepancias)
  COMIDAS_CONTEXTO        tokens de contexto por consulta (por defecto 8192)
  CHAT_MODELO             modelo para el registro tipo chat (por defecto gemma3:27b)
  RUTA_FOTOS              carpeta de las fotos (volumen local)
  RUTA_ALIMENTOS_USDA     base USDA en SQLite generada con preparar_bd.py (opcional)
"""
import argparse
import base64
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from rapidfuzz import fuzz, process

from comun import platos as P
from comun.ajustes import Ajustes
from comun.bd import conectar
from servicios.comidas import chat

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:          # el prototipo importa `config` por su nombre
    sys.path.insert(0, str(_AQUI))
import config as C
import nutricion as N
from coincidencia import Indice

log = logging.getLogger("comidas")
INTERVALO_S = 3
RUTA_FOTOS = Path(os.environ.get("RUTA_FOTOS", "/datos/fotos"))
RONDAS_MAX = 2
CONTEXTO = 8192          # tokens: foto de 1600 px + instrucciones caben de sobra. Con los 32 768 por
                         # defecto, qwen3-vl:32b llena los 32 GB de la GPU y se vuelve lentísimo.
CHAT_MODELO = "gemma3:27b"
MANTENER = "30m"         # tiempo que Ollama deja el modelo cargado tras una consulta


def configurar() -> None:
    """Ajusta el prototipo desde el entorno sin tocar su config.py."""
    e = os.environ.get
    C.OLLAMA_URL = (e("OLLAMA_URL") or C.OLLAMA_URL).rstrip("/")
    if e("COMIDAS_MODELOS"):
        C.MODELOS = C.MODELOS_TEXTO = [m.strip() for m in e("COMIDAS_MODELOS").split(",") if m.strip()]
    global RONDAS_MAX, CHAT_MODELO
    CHAT_MODELO = e("CHAT_MODELO") or CHAT_MODELO
    global CONTEXTO
    if e("COMIDAS_CONTEXTO"):
        CONTEXTO = int(e("COMIDAS_CONTEXTO"))
    if e("COMIDAS_RONDAS_MAX"):
        RONDAS_MAX = max(1, int(e("COMIDAS_RONDAS_MAX")))
    C.RUTA_BD = e("RUTA_ALIMENTOS_USDA") or str(_AQUI / C.RUTA_BD)
    C.RUTA_PROPIOS = e("RUTA_ALIMENTOS_PROPIOS") or str(_AQUI / C.RUTA_PROPIOS)


def consultar_ollama(modelo: str, prompt: str, imagen_b64: str | None = None) -> dict:
    """Como nutricion.consultar, con contexto acotado, modelo cargado un rato y tiempo en el log."""
    msg = {"role": "user", "content": prompt}
    if imagen_b64:
        msg["images"] = [imagen_b64]
    t0 = time.monotonic()
    r = requests.post(f"{C.OLLAMA_URL}/api/chat", timeout=C.TIMEOUT, json={
        "model": modelo, "messages": [msg], "format": N.ESQUEMA, "stream": False, "keep_alive": MANTENER,
        "options": {"temperature": C.TEMPERATURA, "num_ctx": CONTEXTO}})
    r.raise_for_status()
    d = r.json()
    log.info("  %s: %.1f s (carga %.1f s, %s tokens)", modelo, time.monotonic() - t0,
             (d.get("load_duration") or 0) / 1e9, d.get("eval_count"))
    return json.loads(d["message"]["content"])


def discrepan(corridas: list[dict]) -> bool:
    """Mismo criterio que la alerta del prototipo: hidratos (las dos vías de cada consulta) que se
    separan más de ALERTA_ABS_G gramos o de ALERTA_REL de la mediana."""
    hc = [N.hc_bolo(c[via]) for c in corridas for via in ("via_bd", "via_modelo")]
    if len(hc) < 2:
        return False
    med = sorted(hc)[len(hc) // 2] if len(hc) % 2 else sum(sorted(hc)[len(hc) // 2 - 1:len(hc) // 2 + 1]) / 2
    return max(hc) - min(hc) > max(C.ALERTA_ABS_G, C.ALERTA_REL * med)


def fuente_foto(nota: str) -> str:
    """Con descripción, manda la de Daniel para saber QUÉ hay; la foto, para CUÁNTO (19-11-2026)."""
    nota = (nota or "").strip()
    if not nota:
        return N.FUENTE_FOTO.format(nota="")
    return ("Analiza la foto del plato. Quien va a comer dice que es: «" + nota.replace("«", "").replace("»", "") + "».\n"
            "Fíate de esa descripción para saber qué alimentos hay y llámalos así (aunque en la foto parezcan "
            "otra cosa); usa la foto sobre todo para estimar las cantidades. Si la descripción da cantidades, úsalas.")


# Bases abiertas además de USDA, por orden de preferencia (servicios/comidas/fuentes_alimentos.py).
FUENTES_EXTRA = (("ciqual", "CIQUAL"), ("cofid", "CoFID"))
VENTAJA = 3      # puntos de similitud: una base anterior gana a una posterior si está a esta distancia


class TablaVarias(N.TablaAlimentos):
    """La tabla del prototipo (alimentos propios en español, luego USDA) con CIQUAL y CoFID.

    Orden: alimentos propios (nombre en español) → la mejor coincidencia del nombre en inglés entre
    CIQUAL, CoFID y USDA (coincidencia.py: todas las palabras, penalizando las que cambian el
    alimento; antes era un parecido difuso que daba disparates). Si dos bases coinciden casi igual (a VENTAJA puntos), gana la primera:
    CIQUAL y CoFID traen más platos preparados y europeos que USDA. Sin modelos: solo texto."""

    def __init__(self, ruta_bd=None, ruta_propios=None, carpeta_extra: Path | None = None):
        super().__init__(ruta_bd or C.RUTA_BD, ruta_propios or C.RUTA_PROPIOS)
        self.bases = []                                   # [(etiqueta, filas, índice)]
        carpeta = carpeta_extra or Path(C.RUTA_BD).parent
        for archivo, etiqueta in FUENTES_EXTRA:
            ruta = carpeta / f"{archivo}.sqlite"
            if ruta.exists():
                with sqlite3.connect(ruta) as con:
                    con.row_factory = sqlite3.Row
                    filas = [dict(r) | {"fuente": etiqueta} for r in con.execute("SELECT * FROM alimentos")]
                self.bases.append((etiqueta, filas, Indice([f["nombre"] for f in filas])))
        if self.usda:
            self.bases.append(("USDA", self.usda, Indice(self._nombres_usda)))

    def resumen(self) -> str:
        return ", ".join(f"{e} {len(f)}" for e, f, _ in self.bases) or "ninguna"

    def buscar(self, nombre_es: str, nombre_en: str):
        if self.propios and nombre_es:
            m = process.extractOne(nombre_es.lower(), [p[0] for p in self.propios], scorer=fuzz.WRatio)
            if m and m[1] >= max(C.UMBRAL_COINCIDENCIA, 88):
                return self.propios[m[2]][1], m[1]
        if not nombre_en:
            return None, 0
        candidatos = []                                   # (puntos, orden, fila)
        for orden, (_, filas, indice) in enumerate(self.bases):
            m = indice.mejor(nombre_en)
            if m is not None:
                candidatos.append((round(m[1]), orden, filas[m[0]]))
        if not candidatos:
            return None, 0
        mejor = max(c[0] for c in candidatos)
        punt, _, f = min((c for c in candidatos if c[0] >= mejor - VENTAJA), key=lambda c: c[1])
        return f, punt


def limpiar_alertas(alertas: list[str], tabla) -> list[str]:
    """Sin ninguna base de alimentos, el aviso de "sin coincidencia" saldría en todos los platos: se
    cambia por uno solo y claro."""
    if getattr(tabla, "bases", None) or getattr(tabla, "usda", None):
        return alertas
    otras = [a for a in alertas if not a.startswith("Sin coincidencia en la base")]
    if len(otras) != len(alertas):
        otras.append("Valores de los modelos: aún no hay base de alimentos USDA para contrastarlos.")
    return otras


def analizar_adaptativo(tabla, imagen: bytes | None = None, nota: str = "", texto: str | None = None,
                        progreso=log.info, consultar=None) -> dict:
    """Una consulta por modelo; si discrepan, otra ronda (como mucho RONDAS_MAX). La segunda ronda
    va en orden inverso para empezar por el modelo que sigue cargado en la GPU."""
    consultar = consultar or consultar_ollama
    if imagen is not None:
        b64 = base64.b64encode(imagen).decode()
        prompt = N.INSTRUCCIONES.format(fuente=fuente_foto(nota))
        modelos = list(C.MODELOS)
    else:
        b64 = None
        prompt = N.INSTRUCCIONES.format(fuente=N.FUENTE_TEXTO.format(texto=texto))
        modelos = list(C.MODELOS_TEXTO)
    corridas, errores, rondas = [], [], 0
    while rondas < RONDAS_MAX and (rondas == 0 or discrepan(corridas) or not corridas):
        orden = modelos if rondas % 2 == 0 else modelos[::-1]
        rondas += 1
        for m in orden:
            progreso(f"  {m} (ronda {rondas})…")
            try:
                r = N.procesar_respuesta(consultar(m, prompt, b64), tabla)
                r["modelo_id"] = m
                corridas.append(r)
            except Exception as e:
                errores.append(f"{m}: {e}")
    if not corridas:
        raise RuntimeError("Ningún modelo respondió:\n" + "\n".join(errores))
    r = N.consolidar(corridas, errores, origen="foto" if imagen is not None else "texto")
    r["alertas"] = limpiar_alertas(r["alertas"], tabla)
    r["rondas"] = rondas
    if rondas > 1:
        r["alertas"].insert(0, f"Los modelos discrepaban: se repitió el análisis ({rondas} rondas).")
    return r


def resumen(r: dict) -> dict:
    """De la salida del prototipo a las columnas de `comidas`."""
    c = r["consenso"]
    items = [{"nombre": i["nombre"], "gramos": round(i["gramos"]),
              **{k: round(i["por100"][k] * i["gramos"] / 100, 1) for k in ("hidratos", "fibra", "grasa", "proteina")},
              "fuente": i["fuente"]} for i in r["items"]]
    lo, hi = r["rango_hc"]
    return {"hidratos_g": round(c["hidratos"], 1), "fibra_g": round(c["fibra"], 1), "azucares_g": round(c["azucares"], 1),
            "grasa_g": round(c["grasa"], 1), "proteina_g": round(c["proteina"], 1), "kcal": round(c["kcal"], 1),
            "hidratos_bolo_g": round(r["hc_bolo"], 1), "rango_hidratos_min_g": round(min(lo, r["hc_bolo"]), 1),
            "rango_hidratos_max_g": round(max(hi, r["hc_bolo"]), 1), "items": items,
            "estimaciones": {"estimaciones": r["estimaciones"], "errores": r["errores"],
                             "modelos_items": r["modelos_items"], "modelos": C.MODELOS,
                             "rondas": r.get("rondas")},
            "alertas": r["alertas"]}


def _fecha(ts) -> str:
    return ts.astimezone(ZoneInfo("Europe/Madrid")).strftime("%d-%m")


def datos_plato(plato: dict) -> dict:
    """Comida por texto que coincide con un plato guardado: su valor, sin preguntar a los modelos."""
    hc = float(plato["hidratos_g"])
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    pesado = " (pesado)" if plato["referencia"] else ""
    return {"hidratos_g": hc, "fibra_g": None, "azucares_g": None, "grasa_g": num(plato["grasa_g"]),
            "proteina_g": num(plato["proteina_g"]), "kcal": None, "hidratos_bolo_g": hc, "hidratos_modelo_g": None,
            "rango_hidratos_min_g": hc, "rango_hidratos_max_g": hc,
            "items": [{"nombre": plato["nombre"], "gramos": None, "hidratos": hc, "fuente": "tu plato"}],
            "estimaciones": {"plato_id": plato["id"]}, "plato_id": plato["id"],
            "alertas": [f"Como la última vez ({_fecha(plato['actualizado_en'])}): {hc:.0f} g de tu plato "
                        f"«{plato['nombre']}»{pesado}. Si la ración es distinta, corrige los gramos."]}


def con_plato(datos: dict, plato: dict) -> dict:
    """Foto que coincide con un plato guardado: manda el plato; lo de los modelos queda como
    comprobación de la ración (y para medir su error)."""
    hc, mod = float(plato["hidratos_g"]), datos["hidratos_bolo_g"]
    pesado = " (pesado)" if plato["referencia"] else ""
    aviso = (f"Como la última vez ({_fecha(plato['actualizado_en'])}): {hc:.0f} g de tu plato "
             f"«{plato['nombre']}»{pesado}. Los modelos ven {mod:.0f} g.")
    if abs(hc - mod) > max(10.0, 0.2 * hc):
        aviso += " ¿La ración es distinta? Si es así, corrige los gramos."
    return datos | {
        "hidratos_bolo_g": hc, "hidratos_modelo_g": mod, "plato_id": plato["id"],
        "grasa_g": float(plato["grasa_g"]) if plato["grasa_g"] is not None else datos["grasa_g"],
        "proteina_g": float(plato["proteina_g"]) if plato["proteina_g"] is not None else datos["proteina_g"],
        "rango_hidratos_min_g": round(min(hc, datos["rango_hidratos_min_g"]), 1),
        "rango_hidratos_max_g": round(max(hc, datos["rango_hidratos_max_g"]), 1),
        "estimaciones": datos["estimaciones"] | {"plato_id": plato["id"]},
        "alertas": [aviso, *datos["alertas"]]}


def tomar(con) -> dict | None:
    """Reserva la comida pendiente más antigua (seguro aunque haya dos trabajadores)."""
    with con.transaction():
        return con.execute(
            """UPDATE comidas SET estado_analisis = 'analizando'
               WHERE id = (SELECT id FROM comidas WHERE estado_analisis = 'pendiente'
                           ORDER BY creado_en LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING *""").fetchone()


def analizar(con, comida: dict, tabla, analizar_plato=None) -> dict:
    analizar_plato = analizar_plato or analizar_adaptativo
    inicio = time.monotonic()
    try:
        plato = P.elegir(comida["descripcion"], P.todos(con)) if comida["descripcion"] else None
        if comida["origen"] == "foto":
            imagen = (RUTA_FOTOS / comida["foto_ruta"]).read_bytes()
            r = analizar_plato(tabla, imagen=imagen, nota=comida["descripcion"] or "", progreso=log.info)
            datos = resumen(r) | {"hidratos_modelo_g": None, "plato_id": None}
            datos["hidratos_modelo_g"] = datos["hidratos_bolo_g"]
            if plato:
                datos = con_plato(datos, plato)
        elif plato:
            datos = datos_plato(plato)
        else:
            r = analizar_plato(tabla, texto=comida["descripcion"], progreso=log.info)
            datos = resumen(r) | {"plato_id": None}
            datos["hidratos_modelo_g"] = datos["hidratos_bolo_g"]
        datos["estimaciones"]["segundos"] = round(time.monotonic() - inicio, 1)
    except Exception as e:
        log.warning("Comida %s: %s", comida["id"], e)
        with con.transaction():
            con.execute("""UPDATE comidas SET estado_analisis = 'error', error_analisis = %s, analizada_en = %s
                           WHERE id = %s""", (str(e)[:500], datetime.now(timezone.utc), comida["id"]))
        return {"id": comida["id"], "estado": "error", "error": str(e)}
    with con.transaction():
        con.execute(
            """UPDATE comidas SET hidratos_g = %(hidratos_g)s, fibra_g = %(fibra_g)s, azucares_g = %(azucares_g)s,
                 grasa_g = %(grasa_g)s, proteina_g = %(proteina_g)s, kcal = %(kcal)s,
                 hidratos_bolo_g = %(hidratos_bolo_g)s, hidratos_modelo_g = %(hidratos_modelo_g)s,
                 plato_id = %(plato_id)s, rango_hidratos_min_g = %(rango_hidratos_min_g)s,
                 rango_hidratos_max_g = %(rango_hidratos_max_g)s, items = %(items)s, estimaciones = %(estimaciones)s,
                 alertas = %(alertas)s, estado_analisis = 'lista', error_analisis = NULL, analizada_en = %(ahora)s
               WHERE id = %(id)s AND NOT revisada""",
            datos | {"items": json.dumps(datos["items"], ensure_ascii=False),
                     "estimaciones": json.dumps(datos["estimaciones"], ensure_ascii=False, default=str),
                     "alertas": json.dumps(datos["alertas"], ensure_ascii=False),
                     "ahora": datetime.now(timezone.utc), "id": comida["id"]})
    return {"id": comida["id"], "estado": "lista", "hidratos_bolo_g": datos["hidratos_bolo_g"]}


def vuelta(con, tabla, analizar_plato=None) -> dict | None:
    c = tomar(con)
    return analizar(con, c, tabla, analizar_plato) if c else None


def hidratos_tabla(tabla, alimentos: list[dict]) -> float | None:
    """Hidratos (para el bolo) de los alimentos del chat según la tabla; None si falta alguno."""
    items = [{"nombre": str(a.get("nombre") or ""), "nombre_en": str(a.get("nombre_en") or ""),
              "gramos": a.get("gramos")} for a in alimentos]
    if not items:
        return None
    try:
        r = N.procesar_respuesta({"items": items}, tabla)
    except (TypeError, ValueError):
        return None
    if not all(i["en_bd"] and i["gramos"] > 0 for i in r["items"]):
        return None
    return N.hc_bolo(r["via_bd"])


def vuelta_chat(con, tabla=None) -> dict | None:
    return chat.vuelta(con, lambda texto: chat.consultar(C.OLLAMA_URL, CHAT_MODELO, texto, timeout=C.TIMEOUT),
                       CHAT_MODELO, (lambda al: hidratos_tabla(tabla, al)) if tabla is not None else None)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="comidas")
    sub = p.add_subparsers(dest="orden", required=True)
    sub.add_parser("servicio")
    u = sub.add_parser("una")
    u.add_argument("id", type=int)
    a = p.parse_args(argv)
    configurar()
    tabla = TablaVarias()
    log.info("Modelos %s, hasta %s rondas, chat con %s, en %s; base USDA %s; bases de alimentos: %s",
             C.MODELOS, RONDAS_MAX, CHAT_MODELO, C.OLLAMA_URL, "sí" if tabla.usda else "no", tabla.resumen())
    aj = Ajustes.desde_entorno()
    with conectar(aj.database_url, autocommit=True) as con:
        # Lo que quedó a medias si el trabajador se paró vuelve a la cola.
        con.execute("UPDATE comidas SET estado_analisis = 'pendiente' WHERE estado_analisis = 'analizando'")
        con.execute("UPDATE eventos_chat SET estado_analisis = 'pendiente' WHERE estado_analisis = 'analizando'")
        if a.orden == "una":
            print(json.dumps(analizar(con, con.execute("SELECT * FROM comidas WHERE id = %s", (a.id,)).fetchone(), tabla),
                             ensure_ascii=False))
            return 0
        while True:
            try:
                # Primero las comidas (esperan una dosis); el chat, después.
                if vuelta(con, tabla) is None and vuelta_chat(con, tabla) is None:
                    time.sleep(INTERVALO_S)
            except Exception:
                log.exception("Fallo en la vuelta")
                time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
