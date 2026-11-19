"""Cliente de solo lectura para la API v1 de Nightscout.

El token es de un sujeto con rol "readable" (Admin Tools → Subjects). Se envía como parámetro
`token`, que es lo que acepta la API v1. Nunca se escribe en los registros.
"""
from datetime import datetime, timezone

import httpx

MAX_POR_PETICION = 5000


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ErrorNightscout(RuntimeError):
    pass


class ClienteNightscout:
    def __init__(self, url: str, token: str, cliente: httpx.Client | None = None, timeout: float = 30):
        if not url:
            raise ErrorNightscout("Falta NIGHTSCOUT_URL")
        self.url = url.rstrip("/")
        self.token = token
        self.http = cliente or httpx.Client(timeout=timeout)

    def _get(self, ruta: str, params: dict) -> list[dict]:
        if self.token:
            params = params | {"token": self.token}
        try:
            r = self.http.get(f"{self.url}{ruta}", params=params, headers={"Accept": "application/json"})
            if r.status_code >= 400:
                raise ErrorNightscout(f"{ruta}: HTTP {r.status_code}")
            datos = r.json()
        except ErrorNightscout:
            raise
        except Exception as e:  # red, DNS, JSON mal formado…  (el mensaje puede llevar la URL con el token)
            texto = f"{type(e).__name__}: {e}"
            if self.token:
                texto = texto.replace(self.token, "***")
            raise ErrorNightscout(f"{ruta}: {texto}") from None
        if not isinstance(datos, list):
            raise ErrorNightscout(f"{ruta}: respuesta inesperada")
        return datos

    # ------------------------------------------------------------ entradas (glucosa)
    def entradas(self, desde: datetime, hasta: datetime) -> list[dict]:
        """Entradas con desde <= date < hasta. Si la ventana viene llena, la parte en dos."""
        datos = self._get("/api/v1/entries.json", {
            "find[date][$gte]": _ms(desde), "find[date][$lt]": _ms(hasta), "count": MAX_POR_PETICION})
        if len(datos) >= MAX_POR_PETICION and (hasta - desde).total_seconds() > 60:
            medio = desde + (hasta - desde) / 2
            return self.entradas(desde, medio) + self.entradas(medio, hasta)
        return datos

    # ------------------------------------------------------------ tratamientos
    def tratamientos(self, desde: datetime, hasta: datetime) -> list[dict]:
        datos = self._get("/api/v1/treatments.json", {
            "find[created_at][$gte]": _iso(desde), "find[created_at][$lt]": _iso(hasta),
            "count": MAX_POR_PETICION})
        if len(datos) >= MAX_POR_PETICION and (hasta - desde).total_seconds() > 60:
            medio = desde + (hasta - desde) / 2
            return self.tratamientos(desde, medio) + self.tratamientos(medio, hasta)
        return datos


class EscritorNightscout:
    """Escritura de tratamientos (API v1). Token de un sujeto con rol "careportal".

    Separado del cliente de lectura a propósito: el recolector de glucosa sigue con su token de
    solo lectura y solo el recolector de la pluma tiene permiso para crear tratamientos.
    Nightscout v1 hace upsert por (created_at, eventType): reenviar la misma dosis no la duplica.
    """

    def __init__(self, url: str, token: str, cliente: httpx.Client | None = None, timeout: float = 30):
        if not url or not token:
            raise ErrorNightscout("Faltan NIGHTSCOUT_URL o NIGHTSCOUT_TOKEN_ESCRITURA")
        self.url = url.rstrip("/")
        self.token = token
        self.http = cliente or httpx.Client(timeout=timeout)

    def crear_tratamiento(self, tratamiento: dict) -> str | None:
        """Crea (o actualiza) un tratamiento y devuelve su _id si Nightscout lo da."""
        ruta = "/api/v1/treatments"
        try:
            r = self.http.post(f"{self.url}{ruta}", params={"token": self.token}, json=[tratamiento],
                               headers={"Accept": "application/json"})
        except Exception as e:
            raise ErrorNightscout(f"{ruta}: {type(e).__name__}: {e}".replace(self.token, "***")) from None
        if r.status_code in (401, 403):
            raise ErrorNightscout(f"{ruta}: HTTP {r.status_code} (¿el sujeto tiene rol careportal?)")
        if r.status_code >= 400:
            raise ErrorNightscout(f"{ruta}: HTTP {r.status_code}")
        try:
            datos = r.json()
        except Exception:
            return None
        if isinstance(datos, list) and datos and isinstance(datos[0], dict):
            return datos[0].get("_id")
        if isinstance(datos, dict):
            return datos.get("_id")
        return None
