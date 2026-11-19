"""Trabajador de comidas: analiza con los modelos locales (Ollama) las comidas que deja la API.

  python -m servicios.comidas.trabajador servicio      # bucle: mira la base cada pocos segundos
  python -m servicios.comidas.trabajador una ID        # analiza una comida ya (pruebas)

Usa el núcleo del prototipo (nutricion.py: 3 modelos × repeticiones, dos vías, mediana y alertas)
sin cambiarlo. Solo se comunica con la API por la tabla `comidas` (regla 3 de CLAUDE.md).

REGLA 1: lo que escribe aquí son ESTIMACIONES de nutrientes, nunca unidades de insulina. Esos gramos
no entran en ningún cálculo de dosis hasta que Daniel revisa la comida en la app.

Variables de entorno:
  OLLAMA_URL              http://host.docker.internal:11434 en Docker
  COMIDAS_MODELOS         lista separada por comas (por defecto, la de config.py)
  COMIDAS_REPETICIONES    consultas por modelo (por defecto, la de config.py)
  RUTA_FOTOS              carpeta de las fotos (volumen local)
  RUTA_ALIMENTOS_USDA     base USDA en SQLite generada con preparar_bd.py (opcional)
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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


def configurar() -> None:
    """Ajusta el prototipo desde el entorno sin tocar su config.py."""
    e = os.environ.get
    C.OLLAMA_URL = (e("OLLAMA_URL") or C.OLLAMA_URL).rstrip("/")
    if e("COMIDAS_MODELOS"):
        C.MODELOS = C.MODELOS_TEXTO = [m.strip() for m in e("COMIDAS_MODELOS").split(",") if m.strip()]
    if e("COMIDAS_REPETICIONES"):
        C.REPETICIONES = int(e("COMIDAS_REPETICIONES"))
    C.RUTA_BD = e("RUTA_ALIMENTOS_USDA") or str(_AQUI / C.RUTA_BD)
    C.RUTA_PROPIOS = e("RUTA_ALIMENTOS_PROPIOS") or str(_AQUI / C.RUTA_PROPIOS)


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
                             "repeticiones": C.REPETICIONES},
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
    analizar_plato = analizar_plato or N.analizar_plato
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
    log.info("Modelos %s × %s en %s; base USDA %s", C.MODELOS, C.REPETICIONES, C.OLLAMA_URL,
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
