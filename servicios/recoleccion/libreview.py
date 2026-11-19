"""Cliente de la web de LibreView (API no oficial, por ingeniería inversa, sin garantías).

Lo que usa, tal como lo hace www.libreview.com (comprobado con la cuenta real el 19-11-2026):
  POST {base}/auth/login                 {email, password, trustedDeviceToken}
  POST {base}/auth/continue/2fa/sendcode {isPrimaryMethod: true}          (manda el SMS)
  POST {base}/auth/continue/2fa/result   {code, isPrimaryMethod, rememberDevice: true}
  GET  {base}/user                        datos del paciente
  GET  {base}/reportSettings              umbrales y dispositivos (dataSources)
  POST {base}/reports                     pide el informe "Registro diario" (id 540068)
  GET  poll/channel → poll/{id}           espera a que esté listo y devuelve la URL del informe
  GET  lrs-*.libreview.io/report/540068/{id}?session=<token>  HTML con los datos en JSON

El informe trae, por día, la hora de cada inyección de insulina rápida (`RapidActingInsulinLogged`) y,
por hora, la suma de unidades. Los cebados que se marcan en LibreLink no aparecen.

Reglas: solo lee; nunca sube nada a LibreView ni toca la API de la app (/lsl/api), que dejaría a la
app del iPhone sin sesión. La contraseña sale de despliegue/.env; el token de "dispositivo de
confianza" que da LibreView tras el SMS se guarda en un archivo local (LIBREVIEW_SESION), nunca en el
repositorio ni en los registros.
"""
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from servicios.recoleccion.pluma import DosisPluma, unidades_validas

BASE = "https://api.libreview.io"
INFORME_REGISTRO_DIARIO = 540068
ID_CLIENTE_REGISTRO_DIARIO = 5
TIPO_PLUMA = 90002                 # dataSources[].type de la pluma conectada (NovoPen)
VERSION_WEB = "3.27.0.42"
PASOS_EN_LA_WEB = {"tou", "pp", "verifyemail", "consent"}   # condiciones nuevas, etc.: no es el SMS


log = logging.getLogger("pluma")


class ErrorLibreView(RuntimeError):
    pass


class NecesitaCodigo(ErrorLibreView):
    """LibreView pide el código del SMS (doble factor) para dejar entrar."""


def _base_region(region: str) -> str:
    return f"https://api-{region}.libreview.io" if region else BASE


# ------------------------------------------------------------------ sesión en disco
@dataclass
class Sesion:
    base: str = BASE
    token: str | None = None
    token_expira: float = 0             # epoch
    token_parcial: str | None = None    # el que da el login mientras falta el SMS
    dispositivo_confianza: str | None = None
    id_usuario: str | None = None

    @classmethod
    def cargar(cls, ruta: Path | None) -> "Sesion":
        if ruta and ruta.exists():
            try:
                return cls(**json.loads(ruta.read_text(encoding="utf-8")))
            except Exception:
                pass
        return cls()

    def guardar(self, ruta: Path | None) -> None:
        if not ruta:
            return
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f)
        os.replace(tmp, ruta)


# ------------------------------------------------------------------ cliente
@dataclass
class Informe:
    datos: dict                          # Data del informe
    serie_pluma: str | None = None
    sincronizacion_pluma: datetime | None = None
    extra: dict = field(default_factory=dict)


class ClienteLibreView:
    def __init__(self, email: str, password: str, region: str = "", ruta_sesion: Path | str | None = None,
                 cliente: httpx.Client | None = None, timeout: float = 60, zona: str = "Europe/Madrid",
                 version_web: str = VERSION_WEB, espera_sondeo_s: float = 2.0):
        if not email or not password:
            raise ErrorLibreView("Faltan LIBREVIEW_EMAIL o LIBREVIEW_PASSWORD en despliegue/.env")
        self._email, self._password = email, password
        self.ruta = Path(ruta_sesion) if ruta_sesion else None
        self.sesion = Sesion.cargar(self.ruta)
        if region and self.sesion.base == BASE:
            self.sesion.base = _base_region(region)
        self.zona = ZoneInfo(zona)
        self.version_web = version_web
        self.espera_sondeo_s = espera_sondeo_s
        self.http = cliente or httpx.Client(timeout=timeout)

    # ------------------------------------------------------------ peticiones
    def _cabeceras(self, token: str | None) -> dict:
        c = {"Product": "lv", "NEWYU-LV-Web-Version": self.version_web, "Accept": "application/json",
             "Content-Type": "application/json", "Cache-control": "no-cache", "Pragma": "no-cache"}
        if token:
            c["Authorization"] = f"Bearer {token}"
        if self.sesion.id_usuario:
            c["Account-Id"] = hashlib.sha256(self.sesion.id_usuario.encode()).hexdigest()
        return c

    def _limpiar(self, texto: str) -> str:
        for secreto in (self._password, self._email, self.sesion.token, self.sesion.token_parcial,
                        self.sesion.dispositivo_confianza):
            if secreto:
                texto = texto.replace(secreto, "***")
        return texto

    def _peticion(self, metodo: str, url: str, cuerpo: dict | None = None, token: str | None = None,
                  json_esperado: bool = True):
        if not url.startswith("http"):
            url = f"{self.sesion.base}{url}"
        try:
            r = self.http.request(metodo, url, json=cuerpo, headers=self._cabeceras(token))
        except Exception as e:
            raise ErrorLibreView(self._limpiar(f"{url.split('?')[0]}: {type(e).__name__}: {e}")) from None
        ruta = url.split("?")[0].split("libreview.io")[-1]
        if r.status_code == 401:
            raise ErrorLibreView(f"{ruta}: HTTP 401")
        if r.status_code >= 400:
            raise ErrorLibreView(f"{ruta}: HTTP {r.status_code}")
        if not json_esperado:
            return r.text
        try:
            datos = r.json()
        except Exception:
            raise ErrorLibreView(f"{ruta}: respuesta que no es JSON") from None
        ticket = datos.get("ticket") if isinstance(datos, dict) else None
        if isinstance(ticket, dict) and ticket.get("token") and token and token == self.sesion.token:
            self.sesion.token = ticket["token"]                # LibreView renueva el token en cada respuesta
            self.sesion.token_expira = float(ticket.get("expires") or 0)
            self.sesion.guardar(self.ruta)
        return datos

    # ------------------------------------------------------------ entrada y SMS
    def _aceptar_entrada(self, d: dict) -> None:
        ticket = d.get("authTicket") or {}
        self.sesion.token = ticket.get("token")
        self.sesion.token_expira = float(ticket.get("expires") or 0)
        self.sesion.token_parcial = None
        user = d.get("user") or {}
        self.sesion.id_usuario = user.get("id") or self.sesion.id_usuario
        confianza = d.get("trustedDeviceToken") or d.get("deviceToken") or user.get("trustedDeviceToken")
        if confianza:
            self.sesion.dispositivo_confianza = confianza
        self.sesion.guardar(self.ruta)

    def entrar(self) -> None:
        """Entra con correo, contraseña y el token de dispositivo de confianza.
        Lanza NecesitaCodigo si LibreView pide el SMS."""
        cuerpo = {"email": self._email, "password": self._password,
                  "trustedDeviceToken": self.sesion.dispositivo_confianza or ""}
        datos = self._peticion("POST", "/auth/login", cuerpo)
        d = datos.get("data") or {}
        if datos.get("status") == 0 and d.get("redirect") and d.get("region"):
            self.sesion.base = _base_region(str(d["region"]).lower())
            datos = self._peticion("POST", "/auth/login", cuerpo)
            d = datos.get("data") or {}
        estado = datos.get("status")
        if estado in (1, 2, 3):
            raise ErrorLibreView(f"LibreView rechaza el correo o la contraseña (status {estado})")
        paso = d.get("step")
        if paso:
            tipo = (paso.get("type") if isinstance(paso, dict) else str(paso)) or ""
            ticket = d.get("authTicket") or datos.get("ticket") or {}
            self.sesion.token_parcial = ticket.get("token")
            self.sesion.id_usuario = (d.get("user") or {}).get("id") or self.sesion.id_usuario
            self.sesion.guardar(self.ruta)
            if tipo.lower() in PASOS_EN_LA_WEB:
                raise ErrorLibreView(f"LibreView pide un paso en la web ({tipo}): entra una vez en libreview.com")
            raise NecesitaCodigo("LibreView pide el código del SMS")
        if estado != 0 or not (d.get("authTicket") or {}).get("token"):
            raise ErrorLibreView(f"No se pudo entrar en LibreView (status {estado})")
        self._aceptar_entrada(d)

    def pedir_sms(self) -> str | None:
        """Pide a LibreView que mande el SMS. Devuelve el destino enmascarado si lo da."""
        if not self.sesion.token_parcial:
            self.entrar()                                       # renueva el token parcial
        datos = self._peticion("POST", "/auth/continue/2fa/sendcode", {"isPrimaryMethod": True},
                               token=self.sesion.token_parcial)
        if datos.get("status") not in (0, 1, 2):
            raise ErrorLibreView(f"LibreView no mandó el SMS (status {datos.get('status')})")
        d = datos.get("data") or {}
        destino = d.get("primaryValue") or d.get("destination") or d.get("phone")
        return str(destino)[-4:].rjust(len(str(destino)), "•") if destino else None

    def enviar_codigo(self, codigo: str) -> None:
        if not self.sesion.token_parcial:
            raise ErrorLibreView("El código llegó tarde: hay que pedir otro SMS")
        datos = self._peticion("POST", "/auth/continue/2fa/result",
                               {"code": codigo, "isPrimaryMethod": True, "rememberDevice": True},
                               token=self.sesion.token_parcial)
        d = datos.get("data") or {}
        if datos.get("status") != 0 or not (d.get("authTicket") or {}).get("token"):
            self.sesion.token_parcial = None
            self.sesion.guardar(self.ruta)
            raise ErrorLibreView(f"LibreView no aceptó el código (status {datos.get('status')})")
        self._aceptar_entrada(d)
        if not self.sesion.dispositivo_confianza:
            log.warning("LibreView no devolvió el token de dispositivo de confianza: pedirá otro SMS al volver a entrar")

    def _token(self) -> str:
        if not self.sesion.token or (self.sesion.token_expira and self.sesion.token_expira < time.time() + 60):
            self.entrar()
        return self.sesion.token

    def obtener(self, ruta: str, cuerpo: dict | None = None, metodo: str = "GET"):
        try:
            return self._peticion(metodo, ruta, cuerpo, token=self._token())
        except ErrorLibreView as e:
            if "HTTP 401" not in str(e):
                raise
            self.sesion.token = None                            # token caducado: una sola reentrada
            return self._peticion(metodo, ruta, cuerpo, token=self._token())

    # ------------------------------------------------------------ informe
    def cuerpo_informe(self, usuario: dict, ajustes: dict, hoy: date, dias: int = 14) -> tuple[dict, dict]:
        """Arma la petición del "Registro diario" como la web. Devuelve (cuerpo, datos de la pluma)."""
        fuentes = list((ajustes.get("dataSources") or {}).values())
        plumas = sorted((f for f in fuentes if f.get("type") == TIPO_PLUMA),
                        key=lambda f: f.get("uploadDate") or 0, reverse=True)
        otros = sorted((f for f in fuentes if f.get("type") != TIPO_PLUMA),
                       key=lambda f: f.get("uploadDate") or 0, reverse=True)
        if not plumas:
            raise ErrorLibreView("LibreView no tiene ninguna pluma conectada en esta cuenta")
        primario = otros[0] if otros else plumas[0]
        s = ajustes.get("settings") or {}
        rango = s.get("targetRange") or {}
        fin = int(datetime(hoy.year, hoy.month, hoy.day, tzinfo=timezone.utc).timestamp())
        inicio = fin - (dias - 1) * 86400
        u = usuario
        cuerpo = {
            "Country": u.get("country"), "CultureCode": f"{u.get('uiLanguage', 'es')}-{u.get('country', 'ES')}",
            "CultureCodeCommunication": f"{u.get('communicationLanguage', u.get('uiLanguage', 'es'))}-{u.get('country', 'ES')}",
            "DateFormat": int(u.get("dateFormat") or 2), "TimeFormat": int(u.get("timeFormat") or 2),
            "GlucoseUnits": int(u.get("uom") or 1), "EndDate": fin, "TodayDate": fin,
            "HighGlucoseThreshold": s.get("highThreshold"), "LowGlucoseThreshold": s.get("lowThreshold"),
            "TargetRangeHigh": rango.get("max"), "TargetRangeLow": rango.get("min"),
            "PrimaryDeviceId": primario.get("id"), "PrimaryDeviceTypeId": primario.get("type"),
            "SecondaryDeviceIds": [f.get("id") for f in otros if f is not primario], "TertiaryDeviceIds": [],
            "ConnectedInsulinDeviceIds": [f.get("id") for f in plumas], "ConnectedInsulinDeviceTypeId": TIPO_PLUMA,
            "PatientId": u.get("id"), "PatientFirstName": u.get("firstName"), "PatientLastName": u.get("lastName"),
            "PatientDateOfBirth": u.get("dateOfBirth"), "PrintReportsWithPatientInformation": False,
            "ReportIds": [INFORME_REGISTRO_DIARIO], "ClientReportIDs": [ID_CLIENTE_REGISTRO_DIARIO],
            "StartDates": [inicio], "DailyLogShowMinMax": False, "DailyLogShowMaxKetones": False,
        }
        pluma = plumas[0]
        sinc = datetime.fromtimestamp(pluma["uploadDate"], tz=timezone.utc) if pluma.get("uploadDate") else None
        return cuerpo, {"serie": pluma.get("sn"), "sincronizacion": sinc}

    def informe_registro_diario(self, hoy: date | None = None, dias: int = 14, max_sondeos: int = 30) -> Informe:
        hoy = hoy or datetime.now(self.zona).date()
        usuario = (self.obtener("/user").get("data") or {}).get("user") or {}
        ajustes = self.obtener("/reportSettings").get("data") or {}
        cuerpo, pluma = self.cuerpo_informe(usuario, ajustes, hoy, dias)
        r = self.obtener("/reports", cuerpo, metodo="POST")
        if r.get("status") != 0 or not (r.get("data") or {}).get("url"):
            raise ErrorLibreView(f"LibreView no generó el informe (status {r.get('status')})")
        canal = self.obtener(r["data"]["url"])
        url_sondeo = (canal.get("data") or {}).get("lp") or (canal.get("data") or {}).get("ws")
        if not url_sondeo:
            raise ErrorLibreView("LibreView no dio dónde esperar el informe")
        url_informe = None
        for _ in range(max_sondeos):
            try:
                p = self.obtener(url_sondeo)
            except ErrorLibreView as e:
                if "HTTP 503" in str(e) or "HTTP 504" in str(e):   # aún no está listo
                    time.sleep(self.espera_sondeo_s)
                    continue
                raise
            urls = ((p.get("args") or {}).get("urls") or {})
            url_informe = urls.get(str(ID_CLIENTE_REGISTRO_DIARIO)) or next(
                (v for v in urls.values() if f"/report/{INFORME_REGISTRO_DIARIO}/" in v), None)
            if url_informe:
                break
            time.sleep(self.espera_sondeo_s)
        if not url_informe:
            raise ErrorLibreView("El informe de LibreView no estuvo listo a tiempo")
        html = self._peticion("GET", f"{url_informe}?session={self._token()}", json_esperado=False)
        return Informe(datos=extraer_datos(html), serie_pluma=pluma["serie"],
                       sincronizacion_pluma=pluma["sincronizacion"])


# ------------------------------------------------------------------ lectura del informe
def extraer_datos(html: str) -> dict:
    """Saca el objeto {"Data": ...} incrustado en el HTML del informe."""
    i = html.find('{"Data"')
    if i < 0:
        raise ErrorLibreView("El informe no trae datos (¿ha cambiado el formato de LibreView?)")
    nivel, en_texto, escape = 0, False, False
    for j in range(i, len(html)):
        c = html[j]
        if en_texto:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_texto = False
            continue
        if c == '"':
            en_texto = True
        elif c == "{":
            nivel += 1
        elif c == "}":
            nivel -= 1
            if nivel == 0:
                try:
                    return json.loads(html[i:j + 1])["Data"]
                except Exception:
                    break
    raise ErrorLibreView("No se pudieron leer los datos del informe")


def _hora_local(epoch: float, zona: ZoneInfo) -> datetime:
    """LibreView da la hora local del dispositivo como si fuera UTC."""
    ingenua = datetime.fromtimestamp(float(epoch), tz=timezone.utc).replace(tzinfo=None)
    return ingenua.replace(tzinfo=zona)


def _unidades_hora(h: dict) -> Decimal | None:
    """Unidades de insulina rápida de una hora del informe. Con una sola inyección en esa hora,
    cualquiera de estos campos la representa; se toma el mayor para no sumar dos veces la misma."""
    partes = [h.get("RapidActingInsulinManual") or 0, h.get("RapidActingInsulinTotal") or 0,
              (h.get("RapidActingInsulinMeal") or 0) + (h.get("RapidActingInsulinCorrection") or 0)
              + (h.get("RapidActingInsulinUserChange") or 0)]
    return unidades_validas(max(partes))


@dataclass
class Lectura:
    dosis: list[DosisPluma]
    horas_ambiguas: list[dict]           # {hora, inyecciones, unidades_total}
    sin_unidades: int = 0                # horas con inyección pero sin unidades legibles


def dosis_del_informe(datos: dict, zona: ZoneInfo, serie_pluma: str | None = None) -> Lectura:
    """Una DosisPluma por cada hora con exactamente una inyección; las demás, aparte."""
    dosis, ambiguas, sin_unidades = [], [], 0
    for dia in datos.get("Days") or []:
        horas = {h.get("Hour"): h for h in dia.get("Hours") or []}
        por_hora: dict[int, list[datetime]] = {}
        for ts in dia.get("RapidActingInsulinLogged") or []:
            local = _hora_local(ts, zona)
            por_hora.setdefault(local.hour, []).append(local)
        for hora, lista in sorted(por_hora.items()):
            h = horas.get(hora) or {}
            u = _unidades_hora(h)
            if u is None:
                sin_unidades += 1
                continue
            if len(lista) == 1:
                t = lista[0]
                dosis.append(DosisPluma(ts=t.astimezone(timezone.utc), unidades=u, serie_pluma=serie_pluma,
                                        bruto={"hora_local": t.isoformat(), "fuente": "registro_diario"}))
            else:
                inicio = lista[0].replace(minute=0, second=0, microsecond=0)
                ambiguas.append({"hora": inicio.astimezone(timezone.utc), "unidades_total": u,
                                 "inyecciones": [x.astimezone(timezone.utc) for x in sorted(lista)]})
    return Lectura(dosis=dosis, horas_ambiguas=ambiguas, sin_unidades=sin_unidades)
