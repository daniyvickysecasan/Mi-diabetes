"""Mis platos: la misma estimación para el mismo plato (fase 3).

Cuando Daniel confirma una comida y le da nombre de plato (la app propone la descripción), queda
guardada como plato suyo con los hidratos que confirmó. La próxima vez que escriba lo mismo (en la nota de la foto, en una comida
por texto o en el chat) se usa ese valor en vez de volver a preguntar a los modelos.

La coincidencia la decide este código por texto, nunca un modelo (regla 1). Es conservadora: el
nombre normalizado tiene que coincidir casi exactamente ("bocadillo de jamon" ≈ "Bocadillo de
jamón."), y "bocadillo de jamón grande" NO coincide con "bocadillo de jamón" porque la ración
puede ser distinta. El valor del plato sigue pasando por la revisión de Daniel antes de una dosis.
"""
import re
import unicodedata

from rapidfuzz import fuzz

SIMILITUD_MIN = 92        # 0–100 sobre el nombre normalizado (erratas y tildes, no palabras de más)
LARGO_MIN = 4             # nombres más cortos no se guardan ni se buscan (p. ej. "pan")


def clave(texto: str | None) -> str:
    """Minúsculas, sin tildes ni signos, espacios simples."""
    s = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9ñ ]+", " ", s).split())


def coincide(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a == b or fuzz.ratio(a, b) >= SIMILITUD_MIN)


def elegir(texto: str | None, platos: list[dict]) -> dict | None:
    """El plato que corresponde a una descripción entera, o None. `platos`: filas con `clave`."""
    k = clave(texto)
    if len(k) < LARGO_MIN:
        return None
    candidatos = [p for p in platos if coincide(k, p["clave"])]
    return max(candidatos, key=lambda p: (p["clave"] == k, fuzz.ratio(k, p["clave"]))) if candidatos else None


def mencionado(frase: str | None, platos: list[dict]) -> dict | None:
    """El plato nombrado dentro de una frase del chat ("me he comido un bocadillo de jamón hace
    una hora"): su nombre completo tiene que aparecer como palabras seguidas. Gana el más largo."""
    k = f" {clave(frase)} "
    dentro = [p for p in platos if len(p["clave"]) >= LARGO_MIN and f" {p['clave']} " in k]
    return max(dentro, key=lambda p: len(p["clave"])) if dentro else None


# ------------------------------------------------------------------ base de datos
def todos(con) -> list[dict]:
    return con.execute("SELECT * FROM platos ORDER BY actualizado_en DESC").fetchall()


def guardar_desde_comida(con, comida: dict, nombre: str | None) -> dict | None:
    """Tras revisar una comida, si Daniel le dio nombre de plato: crea o actualiza ese plato. Un
    plato pesado (referencia) no lo pisa una comida sin pesar; solo suma una vez más.
    Devuelve el plato o None si no aplica."""
    nombre = " ".join((nombre or "").split())
    k = clave(nombre)
    hc = comida.get("hidratos_bolo_g")
    if len(k) < LARGO_MIN or hc is None or len(nombre) > 120:
        return None
    existente = elegir(nombre, todos(con))
    if existente is None:
        return con.execute(
            """INSERT INTO platos (nombre, clave, hidratos_g, grasa_g, proteina_g, referencia, comida_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (clave) DO NOTHING RETURNING *""",
            (nombre, k, hc, comida.get("grasa_g"), comida.get("proteina_g"), bool(comida.get("referencia")),
             comida["id"])).fetchone()
    if existente["referencia"] and not comida.get("referencia"):
        return con.execute("UPDATE platos SET veces = veces + 1 WHERE id = %s RETURNING *",
                           (existente["id"],)).fetchone()
    return con.execute(
        """UPDATE platos SET hidratos_g = %s, grasa_g = %s, proteina_g = %s, referencia = %s, comida_id = %s,
             veces = veces + 1, actualizado_en = now()
           WHERE id = %s RETURNING *""",
        (hc, comida.get("grasa_g"), comida.get("proteina_g"), bool(comida.get("referencia")), comida["id"],
         existente["id"])).fetchone()
