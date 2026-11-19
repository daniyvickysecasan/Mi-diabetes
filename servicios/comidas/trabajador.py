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
  RUTA_FOTOS              carpeta de las fotos (volumen local)
  RUTA_ALIMENTOS_USDA     base USDA en SQLite generada con preparar_bd.py (opcional)
"""
import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from comun.ajustes import Ajustes
from comun.bd import conectar

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:          # el prototipo importa `config` por su nombre
    sys.path.insert(0, str(_AQUI))
import config as C
import nutricion as N

log = logging.getLogger("comidas")
INTERVALO_S = 3
RUTA_FOTOS = Path(os.environ.get("RUTA_FOTOS", "/datos/fotos"))
RONDAS_MAX = 2
CONTEXTO = 8192          # tokens: foto de 1600 px + instrucciones caben de sobra. Con los 32 768 por
                         # defecto, qwen3-vl:32b llena los 32 GB de la GPU y se vuelve lentísimo.
MANTENER = "30m"         # tiempo que Ollama deja el modelo cargado tras una consulta


def configurar() -> None:
    """Ajusta el prototipo desde el entorno sin tocar su config.py."""
    e = os.environ.get
    C.OLLAMA_URL = (e("OLLAMA_URL") or C.OLLAMA_URL).rstrip("/")
    if e("COMIDAS_MODELOS"):
        C.MODELOS = C.MODELOS_TEXTO = [m.strip() for m in e("COMIDAS_MODELOS").split(",") if m.strip()]
    global RONDAS_MAX
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


def analizar_adaptativo(tabla, imagen: bytes | None = None, nota: str = "", texto: str | None = None,
                        progreso=log.info, consultar=None) -> dict:
    """Una consulta por modelo; si discrepan, otra ronda (como mucho RONDAS_MAX). La segunda ronda
    va en orden inverso para empezar por el modelo que sigue cargado en la GPU."""
    consultar = consultar or consultar_ollama
    if imagen is not None:
        b64 = base64.b64encode(imagen).decode()
        prompt = N.INSTRUCCIONES.format(fuente=N.FUENTE_FOTO.format(nota=f"\nNota del usuario: {nota}" if nota else ""))
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
        if comida["origen"] == "foto":
            imagen = (RUTA_FOTOS / comida["foto_ruta"]).read_bytes()
            r = analizar_plato(tabla, imagen=imagen, nota=comida["descripcion"] or "", progreso=log.info)
        else:
            r = analizar_plato(tabla, texto=comida["descripcion"], progreso=log.info)
        datos = resumen(r)
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
                 hidratos_bolo_g = %(hidratos_bolo_g)s, rango_hidratos_min_g = %(rango_hidratos_min_g)s,
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


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="comidas")
    sub = p.add_subparsers(dest="orden", required=True)
    sub.add_parser("servicio")
    u = sub.add_parser("una")
    u.add_argument("id", type=int)
    a = p.parse_args(argv)
    configurar()
    tabla = N.TablaAlimentos(C.RUTA_BD, C.RUTA_PROPIOS)
    log.info("Modelos %s, hasta %s rondas, en %s; base USDA %s", C.MODELOS, RONDAS_MAX, C.OLLAMA_URL,
             "sí" if tabla.usda else "no (solo alimentos propios y estimación del modelo)")
    aj = Ajustes.desde_entorno()
    with conectar(aj.database_url, autocommit=True) as con:
        # Lo que quedó a medias si el trabajador se paró vuelve a la cola.
        con.execute("UPDATE comidas SET estado_analisis = 'pendiente' WHERE estado_analisis = 'analizando'")
        if a.orden == "una":
            print(json.dumps(analizar(con, con.execute("SELECT * FROM comidas WHERE id = %s", (a.id,)).fetchone(), tabla),
                             ensure_ascii=False))
            return 0
        while True:
            try:
                if vuelta(con, tabla) is None:
                    time.sleep(INTERVALO_S)
            except Exception:
                log.exception("Fallo en la vuelta")
                time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
