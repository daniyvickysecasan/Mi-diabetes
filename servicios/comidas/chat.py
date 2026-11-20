"""Registro tipo chat: de una frase libre a un evento estructurado e incompleto (fase 3, tarea 4).

  "me estoy tomando unas cervezas con los amigos" → alcohol, empieza ahora, ~2–4 bebidas, incertidumbre alta

El modelo local solo rellena un esquema fijo; las horas las calcula este código a partir de
"hace cuántos minutos" (el modelo no maneja fechas). REGLA 1: el resultado nunca produce dosis ni
entra en un cálculo de dosis. Sin aviso por alcohol (Daniel lo rechazó el 19-11-2026): el alcohol
queda solo como etiqueta.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from comun import platos as P

log = logging.getLogger("chat")

TIPOS = ("comida", "bebida", "alcohol", "ejercicio", "enfermedad", "estres", "sueno", "otro")
NIVELES = ("baja", "media", "alta")
MAX_PASADO_MIN = 24 * 60          # un evento de hace más de un día no se interpreta como tal
MAX_FUTURO_MIN = 12 * 60          # "voy a correr en media hora": hasta 12 h por delante
MAX_DURACION_MIN = 24 * 60

ESQUEMA = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": list(TIPOS)},
        "empieza_hace_min": {"type": ["number", "null"]},
        "dura_min": {"type": ["number", "null"]},
        "hidratos_min_g": {"type": ["number", "null"]},
        "hidratos_max_g": {"type": ["number", "null"]},
        "bebidas": {"type": ["number", "null"]},
        "alimentos": {"type": "array", "items": {"type": "object", "properties": {
            "nombre": {"type": "string"}, "nombre_en": {"type": "string"}, "gramos": {"type": "number"}},
            "required": ["nombre", "nombre_en", "gramos"]}},
        "intensidad": {"type": ["string", "null"], "enum": ["baja", "media", "alta", None]},
        "etiquetas": {"type": "array", "items": {"type": "string"}},
        "incertidumbre": {"type": "string", "enum": list(NIVELES)},
        "resumen": {"type": "string"},
    },
    "required": ["tipo", "empieza_hace_min", "dura_min", "alimentos", "etiquetas", "incertidumbre", "resumen"],
}

INSTRUCCIONES = """Eres el asistente de registro de una persona con diabetes tipo 1. Convierte su mensaje
en UN evento. Responde solo con el JSON pedido. No des consejos ni cifras de insulina.

Campos:
- tipo: comida, bebida (sin alcohol), alcohol, ejercicio, enfermedad, estres, sueno u otro.
- empieza_hace_min: minutos desde que empezó hasta ahora. 0 si es ahora; POSITIVO si ya pasó
  ("hace una hora" = 60, "hace 20 minutos" = 20); negativo si es en el futuro ("en media hora" = -30);
  null si no se puede saber.
- dura_min: duración en minutos si se dice o se deduce con sentido; si no, null.
- hidratos_min_g / hidratos_max_g: solo para comida, bebida o alcohol: un rango amplio y honesto de
  hidratos. null si no aplica.
- alimentos: solo para comida, bebida o alcohol: cada alimento con su nombre en español, su nombre
  en inglés (nombre_en) y los gramos (o ml) de una ración típica en España. [] si no aplica.
- bebidas: número de bebidas alcohólicas, si aplica.
- intensidad: para ejercicio (baja, media, alta), si se puede saber.
- etiquetas: palabras sueltas en minúscula (p. ej. alcohol, ejercicio, cafeina, fiesta, viaje, regla, fiebre).
- incertidumbre: baja solo si el mensaje es concreto; alta si faltan cantidades u horas.
- resumen: frase corta en español, en tercera persona y sin juicios.

Mensaje: «{texto}»"""


def consultar(url: str, modelo: str, texto: str, contexto: int = 4096, timeout: int = 300) -> dict:
    t0 = time.monotonic()
    r = requests.post(f"{url}/api/chat", timeout=timeout, json={
        "model": modelo, "messages": [{"role": "user", "content": INSTRUCCIONES.format(texto=texto)}],
        "format": ESQUEMA, "stream": False, "keep_alive": "30m",
        "options": {"temperature": 0.2, "num_ctx": contexto}})
    r.raise_for_status()
    d = r.json()
    log.info("  chat %s: %.1f s (carga %.1f s)", modelo, time.monotonic() - t0, (d.get("load_duration") or 0) / 1e9)
    return json.loads(d["message"]["content"])


def _num(x, lo: float, hi: float) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


_PASADO = re.compile(r"\bhace\b", re.I)
_FUTURO = re.compile(r"\b(en|dentro de)\s+(\d+|un|una|media|dos|tres|cuatro|cinco|diez|quince|veinte|treinta)\b"
                     r"[^.,;]*?\b(min|minuto|minutos|hora|horas)\b", re.I)


def sentido(texto: str | None) -> str | None:
    """'pasado' o 'futuro' si la frase lo dice sin ambigüedad ("hace una hora", "en media hora")."""
    if not texto:
        return None
    pasado, futuro = bool(_PASADO.search(texto)), bool(_FUTURO.search(texto))
    return "pasado" if pasado and not futuro else "futuro" if futuro and not pasado else None


def normalizar(resp: dict, ts_mensaje: datetime, texto: str | None = None) -> dict:
    """Valida la respuesta del modelo y calcula las horas. Lo que no cuadra se deja vacío.

    Si la frase dice claramente pasado o futuro, el signo lo pone este código, no el modelo
    (gemma3 daba "hace una hora" = -60)."""
    tipo = resp.get("tipo") if resp.get("tipo") in TIPOS else "otro"
    hace = _num(resp.get("empieza_hace_min"), -MAX_PASADO_MIN, MAX_PASADO_MIN)
    if hace is not None:
        s = sentido(texto)
        hace = abs(hace) if s == "pasado" else -abs(hace) if s == "futuro" else hace
        if not -MAX_FUTURO_MIN <= hace <= MAX_PASADO_MIN:
            hace = None
    dura = _num(resp.get("dura_min"), 0, MAX_DURACION_MIN)
    inicio = ts_mensaje - timedelta(minutes=round(hace)) if hace is not None else None
    fin = inicio + timedelta(minutes=round(dura)) if inicio is not None and dura is not None else None
    est = {}
    if tipo in ("comida", "bebida", "alcohol"):
        lo, hi = _num(resp.get("hidratos_min_g"), 0, 500), _num(resp.get("hidratos_max_g"), 0, 500)
        if lo is not None and hi is not None:
            est["hidratos_g"] = [round(min(lo, hi)), round(max(lo, hi))]
    if tipo == "alcohol" and (b := _num(resp.get("bebidas"), 0, 50)) is not None:
        est["bebidas"] = round(b, 1)
    if tipo == "ejercicio" and resp.get("intensidad") in NIVELES:
        est["intensidad"] = resp["intensidad"]
    if dura is not None:
        est["duracion_min"] = round(dura)
    etiquetas = []
    for e in [tipo if tipo not in ("otro", "comida", "bebida") else None, *(resp.get("etiquetas") or [])]:
        e = str(e or "").strip().lower()[:30]
        if e and e not in etiquetas:
            etiquetas.append(e)
    inc = resp.get("incertidumbre") if resp.get("incertidumbre") in NIVELES else "alta"
    if inicio is None and inc == "baja":
        inc = "media"                       # sin hora no puede ser poco incierto
    return {"tipo": tipo, "ts_evento_inicio": inicio, "ts_evento_fin": fin, "estimacion": est,
            "incertidumbre": inc, "etiquetas": etiquetas[:8], "resumen": str(resp.get("resumen") or "")[:200]}


CON_HIDRATOS = ("comida", "bebida", "alcohol")


def hidratos_fiables(con, texto: str, resp: dict, hidratos_tabla=None) -> dict | None:
    """Los hidratos del chat, por orden: tu plato guardado si la frase lo nombra (el mismo valor
    siempre), la tabla de alimentos con los gramos que dice el modelo, o None (se queda el rango
    del modelo). Nunca entran en una dosis."""
    plato = P.mencionado(texto, P.todos(con))
    if plato is not None:
        v = round(float(plato["hidratos_g"]))
        return {"hidratos_g": [v, v], "fuente_hidratos": "plato", "plato": plato["nombre"]}
    if hidratos_tabla is not None:
        v = hidratos_tabla([a for a in (resp.get("alimentos") or []) if isinstance(a, dict)])
        if v is not None:
            return {"hidratos_g": [round(v * 0.8), round(v * 1.2)], "fuente_hidratos": "tabla"}
    return None


def tomar(con) -> dict | None:
    with con.transaction():
        return con.execute(
            """UPDATE eventos_chat SET estado_analisis = 'analizando'
               WHERE id = (SELECT id FROM eventos_chat WHERE estado_analisis = 'pendiente'
                           ORDER BY creado_en LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING *""").fetchone()


def interpretar(con, ev: dict, consultar_modelo, modelo: str, hidratos_tabla=None) -> dict:
    """`consultar_modelo(texto) -> dict` (la respuesta en bruto del modelo).
    `hidratos_tabla(alimentos) -> gramos | None`: hidratos según la tabla de alimentos, si todos están."""
    try:
        resp = consultar_modelo(ev["texto"])
        d = normalizar(resp, ev["ts_mensaje"], ev["texto"])
        if d["tipo"] in CON_HIDRATOS:
            fiable = hidratos_fiables(con, ev["texto"], resp, hidratos_tabla)
            if fiable:
                d["estimacion"] |= fiable
            elif "hidratos_g" in d["estimacion"]:
                d["estimacion"]["fuente_hidratos"] = "modelo"
    except Exception as e:
        log.warning("Chat %s: %s", ev["id"], e)
        con.execute("""UPDATE eventos_chat SET estado_analisis = 'error', error_analisis = %s, analizado_en = %s
                       WHERE id = %s""", (str(e)[:500], datetime.now(timezone.utc), ev["id"]))
        return {"id": ev["id"], "estado": "error"}
    con.execute(
        """UPDATE eventos_chat SET tipo = %(tipo)s, ts_evento_inicio = %(ts_evento_inicio)s,
             ts_evento_fin = %(ts_evento_fin)s, estimacion = %(estimacion)s, incertidumbre = %(incertidumbre)s,
             etiquetas = %(etiquetas)s, resumen = %(resumen)s, modelo = %(modelo)s,
             estado_analisis = 'lista', error_analisis = NULL, analizado_en = %(ahora)s
           WHERE id = %(id)s AND estado = 'pendiente'""",
        d | {"estimacion": json.dumps(d["estimacion"]), "modelo": modelo, "ahora": datetime.now(timezone.utc),
             "id": ev["id"]})
    return {"id": ev["id"], "estado": "lista", "tipo": d["tipo"]}


def vuelta(con, consultar_modelo, modelo: str, hidratos_tabla=None) -> dict | None:
    ev = tomar(con)
    return interpretar(con, ev, consultar_modelo, modelo, hidratos_tabla) if ev else None
