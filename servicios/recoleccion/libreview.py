"""Cliente de la API no oficial de LibreView (ingeniería inversa, sin garantías).

Abbott no publica esta API: puede cambiar o romperse en cualquier momento. Por eso:
  - el cliente solo lee; nunca sube nada a LibreView;
  - las credenciales salen de despliegue/.env y nunca se escriben en registros ni errores;
  - la conversión de registros a DosisPluma está aislada en `normalizar_registro`, con tests,
    para ajustarla en un solo sitio cuando LibreView cambie.

Entrada: POST {base}/llu/auth/login (la misma que usa nightscout-librelink-up). Si la cuenta es de
otra región, LibreView responde {"redirect": true, "region": "eu"} y se repite en api-eu.
Cada respuesta puede traer un "ticket" nuevo; se usa el más reciente.

El endpoint concreto que devuelve las dosis de la pluma aún está por confirmar con la cuenta real
(ver `sondear`): hasta entonces `dosis()` falla con un mensaje claro y el servicio no inventa nada.
"""
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from servicios.recoleccion.pluma import DosisPluma, unidades_validas

BASE = "https://api.libreview.io"

# Endpoint de dosis: se rellena tras el sondeo con la cuenta real (formato "/ruta/{paciente}").
ENDPOINT_DOSIS: str | None = None


class ErrorLibreView(RuntimeError):
    pass


def _base_region(region: str) -> str:
    return f"https://api-{region}.libreview.io" if region else BASE


class ClienteLibreView:
    def __init__(self, email: str, password: str, region: str = "", version: str = "4.16.0",
                 cliente: httpx.Client | None = None, timeout: float = 30, zona: str = "Europe/Madrid"):
        if not email or not password:
            raise ErrorLibreView("Faltan LIBREVIEW_EMAIL o LIBREVIEW_PASSWORD en despliegue/.env")
        self._email, self._password = email, password
        self.base = _base_region(region)
        self.version = version
        self.zona = zona
        self.http = cliente or httpx.Client(timeout=timeout)
        self._token: str | None = None
        self._id_cuenta: str | None = None
        self.id_usuario: str | None = None

    # ------------------------------------------------------------ peticiones
    def _cabeceras(self) -> dict:
        c = {"product": "llu.ios", "version": self.version, "Accept": "application/json",
             "Content-Type": "application/json", "cache-control": "no-cache",
             "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)"}
        if self._token:
            c["Authorization"] = f"Bearer {self._token}"
        if self._id_cuenta:
            c["Account-Id"] = self._id_cuenta
        return c

    def _limpiar(self, texto: str) -> str:
        for secreto in (self._password, self._token, self._email):
            if secreto:
                texto = texto.replace(secreto, "***")
        return texto

    def _peticion(self, metodo: str, ruta: str, cuerpo: dict | None = None) -> dict:
        try:
            r = self.http.request(metodo, f"{self.base}{ruta}", json=cuerpo, headers=self._cabeceras())
        except Exception as e:
            raise ErrorLibreView(self._limpiar(f"{ruta}: {type(e).__name__}: {e}")) from None
        if r.status_code >= 400:
            raise ErrorLibreView(f"{ruta}: HTTP {r.status_code}")
        try:
            datos = r.json()
        except Exception:
            raise ErrorLibreView(f"{ruta}: respuesta que no es JSON") from None
        if isinstance(datos, dict):
            ticket = datos.get("ticket") or {}
            if isinstance(ticket, dict) and ticket.get("token"):
                self._token = ticket["token"]
        return datos

    def entrar(self) -> None:
        datos = self._peticion("POST", "/llu/auth/login", {"email": self._email, "password": self._password})
        d = datos.get("data") or {}
        if d.get("redirect") and d.get("region"):
            self.base = _base_region(str(d["region"]).lower())
            datos = self._peticion("POST", "/llu/auth/login", {"email": self._email, "password": self._password})
            d = datos.get("data") or {}
        estado = datos.get("status")
        if estado == 4:
            raise ErrorLibreView("LibreView pide aceptar condiciones nuevas: entra una vez en la app y acéptalas")
        if estado == 2 or not d.get("authTicket"):
            raise ErrorLibreView(f"No se pudo entrar en LibreView (status {estado})")
        self._token = d["authTicket"]["token"]
        self.id_usuario = (d.get("user") or {}).get("id")
        self._id_cuenta = hashlib.sha256(self.id_usuario.encode()).hexdigest() if self.id_usuario else None

    def obtener(self, ruta: str) -> dict:
        if not self._token:
            self.entrar()
        try:
            return self._peticion("GET", ruta)
        except ErrorLibreView as e:
            if "HTTP 401" not in str(e):
                raise
            self._token = None                       # token caducado: una sola reentrada
            self.entrar()
            return self._peticion("GET", ruta)

    # ------------------------------------------------------------ dosis
    def dosis(self, desde: datetime, hasta: datetime) -> list[DosisPluma]:
        if not ENDPOINT_DOSIS:
            raise ErrorLibreView("Endpoint de dosis aún sin confirmar: lanza `python -m servicios.recoleccion.pluma_cli sondear`")
        if not self._token:
            self.entrar()
        datos = self.obtener(ENDPOINT_DOSIS.format(paciente=self.id_usuario or ""))
        registros = _lista(datos.get("data"))
        zona = ZoneInfo(self.zona)
        salida = []
        for r in registros:
            d = normalizar_registro(r, zona)
            if d and desde <= d.ts < hasta:
                salida.append(d)
        return salida


def _lista(datos) -> list[dict]:
    if isinstance(datos, list):
        return [x for x in datos if isinstance(x, dict)]
    if isinstance(datos, dict):
        for k in ("insulin", "insulinEntries", "logbook", "entries", "records", "data"):
            if isinstance(datos.get(k), list):
                return [x for x in datos[k] if isinstance(x, dict)]
    return []


# ------------------------------------------------------------------ normalización
_CLAVES_UNIDADES = ("units", "Units", "insulinUnits", "Value", "value", "dose", "amount")
_CLAVES_HORA_UTC = ("FactoryTimestamp", "timestampUtc", "utcTimestamp")
_CLAVES_HORA_LOCAL = ("Timestamp", "timestamp", "timestampLocal", "dateTime")


def _hora(texto: str, zona: ZoneInfo, es_utc: bool) -> datetime | None:
    """Acepta ISO 8601 y el formato de LibreLinkUp '11/18/2026 9:15:00 PM'."""
    texto = str(texto).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        for formato in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(texto, formato)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if es_utc else zona)
    return dt.astimezone(timezone.utc)


def normalizar_registro(r: dict, zona: ZoneInfo) -> DosisPluma | None:
    """Registro de LibreView → DosisPluma, o None si no es una dosis de insulina.

    Prefiere la hora UTC de fábrica a la local (la local depende de la zona del iPhone).
    PROVISIONAL: los nombres de campo se confirman con el sondeo de la cuenta real.
    """
    hora = None
    for k in _CLAVES_HORA_UTC:
        if r.get(k):
            hora = _hora(r[k], zona, es_utc=True)
            break
    if hora is None:
        for k in _CLAVES_HORA_LOCAL:
            if r.get(k):
                hora = _hora(r[k], zona, es_utc=False)
                break
    unidades = None
    for k in _CLAVES_UNIDADES:
        if r.get(k) is not None:
            unidades = unidades_validas(r[k])
            break
    if hora is None or unidades is None:
        return None
    cebado = bool(r.get("priming") or r.get("isPriming") or r.get("airShot"))
    serie = r.get("penSerialNumber") or r.get("serialNumber") or r.get("SerialNumber")
    ident = r.get("id") or r.get("recordNumber") or r.get("RecordNumber")
    return DosisPluma(ts=hora, unidades=unidades, serie_pluma=str(serie) if serie else None,
                      id_libreview=str(ident) if ident not in (None, "") else None, cebado=cebado, bruto=r)


# ------------------------------------------------------------------ sondeo
CANDIDATOS = [
    "/llu/connections",
    "/llu/connections/{paciente}/graph",
    "/llu/connections/{paciente}/logbook",
    "/user",
    "/account",
    "/glucoseHistory?numPeriods=1&period=14",
    "/reportSettings",
]


def forma(valor, profundidad: int = 0):
    """Estructura de una respuesta sin sus valores: claves, tipos y longitudes. Nada de datos."""
    if profundidad > 6:
        return "…"
    if isinstance(valor, dict):
        return {k: forma(v, profundidad + 1) for k, v in valor.items()}
    if isinstance(valor, list):
        if not valor:
            return []
        return [f"{len(valor)} elementos", forma(valor[0], profundidad + 1)]
    if isinstance(valor, bool):
        return "bool"
    if isinstance(valor, (int, float)):
        return "número"
    if isinstance(valor, str):
        return f"texto({len(valor)})"
    return type(valor).__name__ if valor is not None else None


def sondear(cliente: ClienteLibreView) -> dict:
    """Prueba los endpoints candidatos y devuelve solo la forma de cada respuesta."""
    cliente.entrar()
    salida = {"region": cliente.base, "entrada": "ok"}
    conexiones = []
    for ruta in CANDIDATOS:
        if "{paciente}" in ruta:
            if not conexiones:
                salida[ruta] = "sin conexiones de LibreLinkUp"
                continue
            ruta_real = ruta.format(paciente=conexiones[0])
        else:
            ruta_real = ruta
        try:
            datos = cliente.obtener(ruta_real)
            salida[ruta] = forma(datos)
            if ruta == "/llu/connections":
                conexiones = [c.get("patientId") for c in _lista(datos.get("data")) if c.get("patientId")]
        except ErrorLibreView as e:
            salida[ruta] = f"error: {e}"
    return salida
