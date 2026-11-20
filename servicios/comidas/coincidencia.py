"""Coincidencia de nombres de alimentos en inglés (CIQUAL, CoFID, USDA). Determinista, sin modelos.

El parecido difuso de cadenas (WRatio) daba disparates: "cooked white rice" → "Egg white, cooked" o
"Kir (white wine…)". Aquí cada palabra de lo que dice el modelo tiene que estar en el nombre de la
base (admite plurales y erratas), y las palabras de más restan: poco si no cambian el alimento
("raw", "homemade", "flesh"), mucho si lo transforman ("dried", "juice", "sauce", "brandy"…), para
que "apple" no acabe en "Apple, dried" (64 g de hidratos por 100 g en vez de 13). Por debajo de
UMBRAL no hay coincidencia y se usa el valor del modelo (con su aviso de "sin coincidencia").
"""
from rapidfuzz import fuzz, utils

VACIAS = {"and", "with", "of", "in", "the", "a", "an", "or", "for", "from", "to", "on", "no", "added", "without",
          "type", "style", "e", "g", "eg", "etc", "ex", "average", "food"}
NEUTRAS = {"raw", "fresh", "homemade", "home", "made", "flesh", "skin", "peeled", "unpeeled", "whole", "plain",
           "regular", "standard", "natural", "salt", "unsalted", "salted", "water", "cooked", "boiled", "drained",
           "prepacked", "retail", "medium", "large", "small", "ripe", "edible", "portion", "weighed", "only",
           "average", "generic", "basic", "classic", "mixed", "assorted", "white", "red", "green", "yellow",
           "uk", "french", "british"}
TRANSFORMAN = {"dried", "dehydrated", "juice", "nectar", "concentrate", "concentrated", "powder", "powdered",
               "brandy", "liqueur", "wine", "spirit", "flour", "starch", "chip", "crisp", "sauce", "soup", "canned",
               "jam", "jelly", "syrup", "sweetened", "candied", "fried", "cake", "pie", "tart", "biscuit", "cookie",
               "bar", "milkshake", "puree", "compote", "flake", "bran", "oil", "fat", "spread", "paste",
               "extract", "sugar", "honey", "chocolate", "cream", "ice", "frozen", "instant", "breaded", "batter",
               "crumbed", "stuffed", "filled", "sandwich", "salad", "pizza", "burger", "sausage", "egg", "cheese",
               "milk", "yogurt", "yoghurt", "wrap", "kebab", "curry", "stew", "risotto", "pudding", "bread",
               "pastry", "dessert", "evaporated", "condensed", "roast", "ring", "nugget", "finger", "sweet", "light", "diet", "reduced", "low", "free", "skimmed", "semi"}
PESO_NEUTRA, PESO_OTRA, PESO_TRANSFORMA = 1.0, 7.0, 20.0
PESO_CABEZA = 6.0        # la primera palabra del nombre (el alimento en estas bases) no es de la consulta:
                         # "apple" → "Chutney, apple" pierde frente a "Apple, raw"
UMBRAL = 82.0


def raiz(p: str) -> str:
    if len(p) > 4 and p.endswith("ies"):
        return p[:-3] + "y"
    if len(p) > 4 and p.endswith(("oes", "ches", "shes", "xes")):
        return p[:-2]
    if len(p) > 3 and p.endswith("s") and not p.endswith("ss"):
        return p[:-1]
    return p


def palabras(texto: str) -> list[str]:
    return [raiz(p) for p in utils.default_process(texto or "").split() if p not in VACIAS and not p.isdigit()]


def _igual(a: str, b: str) -> bool:
    return a == b or (len(a) >= 5 and len(b) >= 5 and fuzz.ratio(a, b) >= 85)


def puntos(consulta: list[str], nombre: list[str]) -> float:
    """0–100. 0 si falta alguna palabra de la consulta en el nombre."""
    if not consulta or not nombre:
        return 0.0
    usadas = set()
    for q in consulta:
        i = next((i for i, n in enumerate(nombre) if i not in usadas and _igual(q, n)), None)
        if i is None:
            return 0.0
        usadas.add(i)
    coste = 0.0 if 0 in usadas else PESO_CABEZA
    for i, n in enumerate(nombre):
        if i in usadas:
            continue
        coste += PESO_NEUTRA if n in NEUTRAS else PESO_TRANSFORMA if n in TRANSFORMAN else PESO_OTRA
    return max(0.0, 100.0 - coste)


class Indice:
    """Nombres de una base ya partidos en palabras, para buscar deprisa."""

    def __init__(self, nombres: list[str]):
        self.palabras = [palabras(n) for n in nombres]

    def mejor(self, consulta: str, umbral: float = UMBRAL) -> tuple[int, float] | None:
        """(posición, puntos) del mejor nombre con al menos `umbral` puntos, o None."""
        q = palabras(consulta)
        if not q:
            return None
        mejor = None
        for i, n in enumerate(self.palabras):
            p = puntos(q, n)
            if p > 0 and (mejor is None or p > mejor[1] or (p == mejor[1] and len(n) < len(self.palabras[mejor[0]]))):
                mejor = (i, p)
        return mejor if mejor is not None and mejor[1] >= umbral else None
