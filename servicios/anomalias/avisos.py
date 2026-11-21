"""Envía las anomalías pendientes de avisar: Web Push a la PWA y, para lo urgente, también ntfy.

Canales (decisión de Daniel del 21-11-2026):
- Web Push: a cada iPhone/navegador que activó los avisos en la PWA (`suscripciones_push`). Al
  pulsar la notificación se abre la PWA en Avisos (con ntfy, iOS abría el navegador).
- ntfy: solo las urgentes (acumulación de insulina), como respaldo (AVISOS_NTFY=urgentes; también
  "todos" o "ninguno"). Si Web Push no llega a ningún dispositivo, el aviso sale por ntfy igualmente.

Reglas de envío:
- Urgentes: al momento, también de noche y sin límite diario.
- No urgentes: esperan al final del silencio nocturno (`avisar_desde`) y como mucho
  `max_avisos_dia` al día; las que pasan del límite se quedan solo en la app (`retenido`).
- Una pregunta ya respondida o descartada no se envía. Una que lleva demasiado esperando
  (servicio parado, sin canales) se retiene en vez de llegar tarde.

Se responde en la app (detrás de Access y la passkey); ningún canal recibe respuestas.
"""
import json
import logging
from datetime import datetime, timedelta

import httpx

from servicios.anomalias import reglas as R

log = logging.getLogger("anomalias.avisos")
CADUCA_URGENTE = timedelta(minutes=30)
CADUCA_NORMAL = timedelta(hours=12)
MAX_INTENTOS = 5
MAX_ERRORES_SUSCRIPCION = 10


class ClienteNtfy:
    def __init__(self, url: str, tema: str, token: str = "", cliente: httpx.Client | None = None):
        if not url or not tema:
            raise ValueError("Faltan NTFY_URL o NTFY_TEMA")
        self.url, self.tema = url.rstrip("/"), tema
        self.http = cliente or httpx.Client(timeout=15)
        self.cabeceras = {"Authorization": f"Bearer {token}"} if token else {}

    def publicar(self, titulo: str, texto: str, urgente: bool = False, enlace: str | None = None) -> None:
        cuerpo = {"topic": self.tema, "title": titulo, "message": texto, "priority": 5 if urgente else 3,
                  "tags": ["warning"] if urgente else ["question"]}
        if enlace:
            cuerpo |= {"click": enlace, "actions": [{"action": "view", "label": "Responder", "url": enlace}]}
        r = self.http.post(self.url + "/", json=cuerpo, headers=self.cabeceras)
        r.raise_for_status()


class SuscripcionCaducada(Exception):
    """El servicio de push dice que esa suscripción ya no existe (404/410): se desactiva."""


class ClienteWebPush:
    """Web Push con claves VAPID (pywebpush). El contenido va cifrado de extremo a extremo: el
    servicio de push del navegador (Apple en el iPhone) no puede leerlo."""

    def __init__(self, clave_privada: str, contacto: str, enviar=None):
        if not clave_privada or not contacto:
            raise ValueError("Faltan VAPID_PRIVADA o URL_APP")
        self.clave, self.contacto = clave_privada, contacto
        if enviar is None:
            from pywebpush import webpush as enviar
        self.enviar = enviar

    def publicar(self, suscripcion: dict, carga: dict, urgente: bool = False) -> None:
        from pywebpush import WebPushException
        try:
            self.enviar(subscription_info={"endpoint": suscripcion["endpoint"],
                                           "keys": {"p256dh": suscripcion["p256dh"], "auth": suscripcion["auth"]}},
                        data=json.dumps(carga, ensure_ascii=False), vapid_private_key=self.clave,
                        vapid_claims={"sub": self.contacto}, ttl=1800 if urgente else 43200,
                        headers={"Urgency": "high" if urgente else "normal"}, timeout=15)
        except WebPushException as e:
            codigo = getattr(getattr(e, "response", None), "status_code", None)
            if codigo in (404, 410):
                raise SuscripcionCaducada(str(codigo)) from e
            raise


def _inicio_dia_local(ahora: datetime) -> datetime:
    local = ahora.astimezone(R.ZONA)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _por_webpush(con, webpush: ClienteWebPush, a: dict, ahora: datetime) -> tuple[int, list[str]]:
    """Envía a todas las suscripciones activas. Devuelve (entregados, errores)."""
    carga = {"id": a["id"], "titulo": a["titulo"], "texto": f"{a['texto']}\n{a['pregunta']}",
             "urgente": a["urgente"], "url": "/#avisos"}
    ok, errores = 0, []
    for s in con.execute("SELECT * FROM suscripciones_push WHERE activa ORDER BY id").fetchall():
        try:
            webpush.publicar(s, carga, a["urgente"])
        except SuscripcionCaducada as e:
            con.execute("""UPDATE suscripciones_push SET activa = false, ultimo_error = %s WHERE id = %s""",
                        (f"caducada ({e})", s["id"]))
            continue
        except Exception as e:
            n = s["errores"] + 1
            con.execute("""UPDATE suscripciones_push SET errores = %s, ultimo_error = %s, activa = %s WHERE id = %s""",
                        (n, str(e)[:300], n < MAX_ERRORES_SUSCRIPCION, s["id"]))
            errores.append(str(e)[:300])
            continue
        con.execute("UPDATE suscripciones_push SET ultimo_ok = %s, errores = 0, ultimo_error = NULL WHERE id = %s",
                    (ahora, s["id"]))
        ok += 1
    return ok, errores


def enviar_pendientes(con, ntfy: ClienteNtfy | None, ahora: datetime, u: R.Umbrales = R.UMBRALES,
                      url_app: str = "", webpush: ClienteWebPush | None = None,
                      modo_ntfy: str = "urgentes") -> dict:
    res = {"enviados": 0, "retenidos": 0, "errores": 0, "no_enviar": 0}
    filas = con.execute(
        """SELECT * FROM anomalias WHERE aviso_estado = 'pendiente' AND avisar_desde <= %s
           ORDER BY urgente DESC, detectada_en""", (ahora,)).fetchall()
    enviados_hoy = con.execute(
        """SELECT count(*) AS n FROM anomalias WHERE aviso_estado = 'enviado' AND NOT urgente AND avisado_en >= %s""",
        (_inicio_dia_local(ahora),)).fetchone()["n"]
    for a in filas:
        def marcar(estado, error=None):
            con.execute("""UPDATE anomalias SET aviso_estado = %s, aviso_error = coalesce(%s, aviso_error),
                           actualizada_en = now() WHERE id = %s""", (estado, error, a["id"]))
        if a["estado"] != "abierta":
            marcar("no_enviar")
            res["no_enviar"] += 1
            continue
        if ahora - a["avisar_desde"] > (CADUCA_URGENTE if a["urgente"] else CADUCA_NORMAL):
            marcar("retenido", "llegaba tarde")
            res["retenidos"] += 1
            continue
        if not a["urgente"] and enviados_hoy >= u.max_avisos_dia:
            marcar("retenido", "límite diario")
            res["retenidos"] += 1
            continue
        if ntfy is None and webpush is None:
            continue
        canales, errores = {}, []
        if webpush is not None:
            ok, err = _por_webpush(con, webpush, a, ahora)
            errores += err
            if ok:
                canales["webpush"] = ok
        quiere_ntfy = modo_ntfy == "todos" or (modo_ntfy == "urgentes" and a["urgente"])
        if ntfy is not None and (quiere_ntfy or not canales):     # sin Web Push entregado, ntfy de respaldo
            enlace = f"{url_app}/#avisos" if url_app else None
            try:
                ntfy.publicar(a["titulo"], f"{a['texto']}\n{a['pregunta']}", a["urgente"], enlace)
                canales["ntfy"] = True
                con.execute("UPDATE estado_anomalias SET ntfy_ok = %s, ntfy_error = NULL", (ahora,))
            except Exception as e:      # ntfy caído
                errores.append(f"ntfy: {str(e)[:250]}")
                con.execute("UPDATE estado_anomalias SET ntfy_error = %s", (str(e)[:300],))
                log.warning("ntfy: %s", e)
        if not canales:
            if not errores:             # Web Push sin suscripciones y sin ntfy: se queda en la app
                continue
            n = a["aviso_intentos"] + 1
            con.execute("""UPDATE anomalias SET aviso_intentos = %s, aviso_error = %s, aviso_estado = %s,
                           actualizada_en = now() WHERE id = %s""",
                        (n, "; ".join(errores)[:300], "error" if n >= MAX_INTENTOS else "pendiente", a["id"]))
            res["errores"] += 1
            continue
        con.execute("""UPDATE anomalias SET aviso_estado = 'enviado', avisado_en = %s, aviso_error = NULL,
                       canales = %s, actualizada_en = now() WHERE id = %s""", (ahora, json.dumps(canales), a["id"]))
        res["enviados"] += 1
        if not a["urgente"]:
            enviados_hoy += 1
    return res
