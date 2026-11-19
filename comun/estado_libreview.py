"""Estado de la conexión con LibreView, compartido por la API y el recolector de la pluma.

La API y el recolector no se hablan: se comunican solo por la fila única de `estado_libreview`.

  sin_iniciar ──► ok ◄──────────────────────────────┐
       │          │ (LibreView pide el SMS)          │
       └──────────┴──► necesita_codigo               │
                         │ app: "Enviarme el SMS"    │
                         ▼                           │
                       sms_pedido  (el recolector manda el SMS)
                         ▼                           │
                       esperando_codigo              │
                         │ app: escribe el código    │
                         ▼                           │
                       codigo_recibido ──────────────┘ (o vuelve a necesita_codigo si falla)
  error: LibreView no responde o ha cambiado; el recolector lo reintenta en cada vuelta.

El SMS solo se manda cuando Daniel lo pide en la app (así no llegan SMS a deshoras ni caducan).
El código se borra de la base en cuanto se usa.
"""
import re
from datetime import datetime, timedelta, timezone

PATRON_CODIGO = re.compile(r"^[0-9]{4,8}$")
CADUCIDAD_CODIGO = timedelta(minutes=10)     # sin código en este tiempo: hay que pedir otro SMS
SONDEO_CAIDO = timedelta(hours=1)            # sin sincronizar en este tiempo: avisar


class EstadoNoPermite(ValueError):
    pass


def leer(con) -> dict:
    return con.execute("SELECT * FROM estado_libreview").fetchone()


def poner(con, estado: str, **campos) -> None:
    """Cambia el estado. `campos`: codigo, destino_sms, ultimo_ok, ultima_sincronizacion, error."""
    permitidos = {"codigo", "destino_sms", "ultimo_ok", "ultima_sincronizacion", "error"}
    extra = {k: v for k, v in campos.items() if k in permitidos}
    asignaciones = ", ".join(f"{k} = %({k})s" for k in extra)
    con.execute(f"UPDATE estado_libreview SET estado = %(estado)s, cambiado_en = now()"
                f"{', ' + asignaciones if asignaciones else ''}", {"estado": estado, **extra})


def pedir_sms(con) -> dict:
    """Desde la app: Daniel pide el SMS."""
    e = leer(con)
    if e["estado"] not in ("necesita_codigo", "esperando_codigo", "error", "sin_iniciar"):
        raise EstadoNoPermite("LibreView no está pidiendo el código ahora")
    poner(con, "sms_pedido", codigo=None, destino_sms=None, error=None)
    return leer(con)


def dar_codigo(con, codigo: str) -> dict:
    """Desde la app: Daniel escribe el código que le ha llegado."""
    codigo = (codigo or "").strip().replace(" ", "")
    if not PATRON_CODIGO.match(codigo):
        raise ValueError("El código son de 4 a 8 cifras")
    if leer(con)["estado"] != "esperando_codigo":
        raise EstadoNoPermite("No hay ningún SMS pendiente: pide uno nuevo")
    poner(con, "codigo_recibido", codigo=codigo)
    return leer(con)


def para_la_app(con, ahora: datetime | None = None) -> dict:
    """Lo que ve la app: estado, texto y si hay que mostrar el botón del SMS o la casilla del código.
    Nunca devuelve el código."""
    ahora = ahora or datetime.now(timezone.utc)
    e = leer(con)
    estado = e["estado"]
    if estado == "esperando_codigo" and e["cambiado_en"] < ahora - CADUCIDAD_CODIGO:
        estado = "necesita_codigo"                       # el SMS ya habrá caducado
    r = {"estado": estado, "ultimo_ok": e["ultimo_ok"], "ultima_sincronizacion": e["ultima_sincronizacion"],
         "accion": None, "mensaje": None}
    if estado == "necesita_codigo":
        r |= {"accion": "pedir_sms",
              "mensaje": "LibreView pide el código del SMS para seguir leyendo la pluma."}
    elif estado == "sms_pedido":
        r |= {"accion": "esperar", "mensaje": "Pidiendo el SMS a LibreView…"}
    elif estado == "esperando_codigo":
        destino = f" al {e['destino_sms']}" if e["destino_sms"] else ""
        r |= {"accion": "codigo", "mensaje": f"Escribe el código que te ha llegado por SMS{destino}."}
    elif estado == "codigo_recibido":
        r |= {"accion": "esperar", "mensaje": "Comprobando el código con LibreView…"}
    elif estado == "error":
        r |= {"mensaje": "No se pueden leer las dosis de LibreView: " + (e["error"] or "error desconocido")}
    elif estado == "ok" and e["ultimo_ok"] and e["ultimo_ok"] < ahora - SONDEO_CAIDO:
        r |= {"estado": "caido", "mensaje": "Hace más de una hora que no se leen las dosis de LibreView."}
    return r
