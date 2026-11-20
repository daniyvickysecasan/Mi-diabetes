"""Envía por ntfy (servidor de casa) las anomalías pendientes de avisar.

- Urgentes (acumulación de insulina): al momento, también de noche y sin límite diario.
- No urgentes: esperan al final del silencio nocturno (`avisar_desde`) y como mucho
  `max_avisos_dia` al día; las que pasan del límite se quedan solo en la app (`retenido`).
- Una pregunta ya respondida o descartada no se envía. Una que lleva demasiado esperando
  (servicio parado, ntfy sin configurar) se retiene en vez de llegar tarde.

El aviso lleva el texto y un enlace a la pantalla Avisos de la app; se responde allí (detrás de
Access y la passkey). ntfy no recibe ninguna respuesta.
"""
import logging
from datetime import datetime, timedelta

import httpx

from servicios.anomalias import reglas as R

log = logging.getLogger("anomalias.avisos")
CADUCA_URGENTE = timedelta(minutes=30)
CADUCA_NORMAL = timedelta(hours=12)
MAX_INTENTOS = 5


class ClienteNtfy:
    def __init__(self, url: str, tema: str, token: str = "", cliente: httpx.Client | None = None):
        if not url or not tema:
            raise ValueError("Faltan NTFY_URL o NTFY_TEMA")
        self.url, self.tema = url.rstrip("/"), tema
        cabeceras = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = cliente or httpx.Client(timeout=15)
        self.cabeceras = cabeceras

    def publicar(self, titulo: str, texto: str, urgente: bool = False, enlace: str | None = None) -> None:
        cuerpo = {"topic": self.tema, "title": titulo, "message": texto, "priority": 5 if urgente else 3,
                  "tags": ["warning"] if urgente else ["question"]}
        if enlace:
            cuerpo |= {"click": enlace, "actions": [{"action": "view", "label": "Responder", "url": enlace}]}
        r = self.http.post(self.url + "/", json=cuerpo, headers=self.cabeceras)
        r.raise_for_status()


def _inicio_dia_local(ahora: datetime) -> datetime:
    local = ahora.astimezone(R.ZONA)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def enviar_pendientes(con, ntfy: ClienteNtfy | None, ahora: datetime, u: R.Umbrales = R.UMBRALES,
                      url_app: str = "") -> dict:
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
        if ntfy is None:
            continue
        enlace = f"{url_app}/#avisos" if url_app else None
        try:
            ntfy.publicar(a["titulo"], f"{a['texto']}\n{a['pregunta']}", a["urgente"], enlace)
        except Exception as e:      # ntfy caído: se reintenta en el siguiente ciclo
            n = a["aviso_intentos"] + 1
            con.execute("""UPDATE anomalias SET aviso_intentos = %s, aviso_error = %s, aviso_estado = %s,
                           actualizada_en = now() WHERE id = %s""",
                        (n, str(e)[:300], "error" if n >= MAX_INTENTOS else "pendiente", a["id"]))
            con.execute("UPDATE estado_anomalias SET ntfy_error = %s", (str(e)[:300],))
            log.warning("ntfy: %s", e)
            res["errores"] += 1
            continue
        con.execute("""UPDATE anomalias SET aviso_estado = 'enviado', avisado_en = %s, aviso_error = NULL,
                       actualizada_en = now() WHERE id = %s""", (ahora, a["id"]))
        con.execute("UPDATE estado_anomalias SET ntfy_ok = %s, ntfy_error = NULL", (ahora,))
        res["enviados"] += 1
        if not a["urgente"]:
            enviados_hoy += 1
    return res
