"""Web de LibreView simulada para los tests (httpx.MockTransport). Datos siempre sintéticos.

Imita lo comprobado con la cuenta real: redirección de región, doble factor por SMS con token de
dispositivo de confianza, renovación del token en cada respuesta (`ticket`), informe "Registro diario"
que se espera por sondeo (con algún 503) y se sirve como HTML con el JSON {"Data": ...} incrustado.
"""
import json
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

ID_USUARIO = "usuario-sintetico-1"
SERIE_PLUMA = "PLUMAS"


def _epoch_local(dt_local: datetime) -> int:
    """Como LibreView: la hora local de la pluma contada como si fuera UTC."""
    return int(dt_local.replace(tzinfo=timezone.utc).timestamp())


def datos_informe(inyecciones: list[tuple[datetime, float]]) -> dict:
    """Data del informe a partir de (hora local ingenua o con zona, unidades)."""
    dias: dict = {}
    for t, u in inyecciones:
        t = t.replace(tzinfo=None)
        dia = dias.setdefault(t.date(), {"logged": [], "horas": {}})
        dia["logged"].append(_epoch_local(t))
        dia["horas"][t.hour] = dia["horas"].get(t.hour, Decimal(0)) + Decimal(str(u))
    salida = []
    for d, v in sorted(dias.items()):
        horas = [{"Hour": h, "RapidActingInsulinManual": float(v["horas"].get(h, 0)),
                  "RapidActingInsulinMeal": 0, "RapidActingInsulinCorrection": 0,
                  "RapidActingInsulinUserChange": 0, "RapidActingInsulinTotal": 0,
                  "IsRapidActingInsulinLogged": h in v["horas"], "LongActingInsulin": 0,
                  "notes": "nota con {llaves} y \"comillas\""} for h in range(24)]
        salida.append({"Date": _epoch_local(datetime(d.year, d.month, d.day)), "TimeChanges": [],
                       "Glucose": [], "RapidActingInsulinLogged": sorted(v["logged"]), "Hours": horas,
                       "RapidActingInsulinTotal": float(sum(v["horas"].values()))})
    return {"NoData": False, "DayCount": len(salida), "Days": salida, "Input": {}, "ReportNotAvailable": False}


def html_informe(datos: dict) -> str:
    return ("<html><head><script>var x = {a: 1};</script></head><body><script>window.informe = "
            + json.dumps({"Data": datos}) + ";</script><div>{no es json}</div></body></html>")


class LibreViewSimulado:
    def __init__(self, email="yo@ejemplo.test", password="clave-sintetica", codigo="123456",
                 inyecciones=None, sondeos_503=1, duracion_token_s=7200):
        self.email, self.password, self.codigo = email, password, codigo
        self.inyecciones = list(inyecciones or [])
        self.sondeos_503 = sondeos_503
        self.duracion = duracion_token_s
        self.confianza_valida: set[str] = set()
        self.tokens: set[str] = set()
        self.parciales: set[str] = set()
        self.sms_enviados = 0
        self.logins = 0
        self.informes = 0
        self.fallar_informe = False
        self.peticiones: list[str] = []
        self._n = 0
        self._sondeos = 0

    def transporte(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.responder)

    # ------------------------------------------------------------ ayudas
    def _nuevo(self, prefijo: str) -> str:
        self._n += 1
        return f"{prefijo}-{self._n}"

    def _ticket(self) -> dict:
        t = self._nuevo("web")
        self.tokens.add(t)
        return {"token": t, "expires": int(time.time()) + self.duracion, "duration": self.duracion * 1000}

    def _ok(self, data, ticket=True) -> httpx.Response:
        cuerpo = {"status": 0, "data": data}
        if ticket:
            cuerpo["ticket"] = self._ticket()
        return httpx.Response(200, json=cuerpo)

    def _entrada(self, con_confianza: str | None = None) -> dict:
        d = {"user": {"id": ID_USUARIO, "firstName": "Sintético", "lastName": "Prueba"},
             "authTicket": self._ticket()}
        if con_confianza:
            d["trustedDeviceToken"] = con_confianza
        return d

    # ------------------------------------------------------------ rutas
    def responder(self, req: httpx.Request) -> httpx.Response:
        ruta = req.url.path
        self.peticiones.append(f"{req.method} {req.url.host}{ruta}")
        cuerpo = json.loads(req.content) if req.content else {}
        bearer = req.headers.get("Authorization", "").removeprefix("Bearer ")
        if req.headers.get("Product") != "lv":
            return httpx.Response(400, json={"status": 920, "error": {"message": "ProductMismatch"}})

        if req.url.host == "lrs-eu.libreview.io":
            if req.url.params.get("session") not in self.tokens:
                return httpx.Response(401)
            return httpx.Response(200, text=html_informe(datos_informe(self.inyecciones)))
        if req.url.host == "api.libreview.io":
            return httpx.Response(200, json={"status": 0, "data": {"redirect": True, "region": "eu"}})

        if ruta == "/auth/login":
            self.logins += 1
            if cuerpo.get("email") != self.email or cuerpo.get("password") != self.password:
                return httpx.Response(200, json={"status": 2, "error": {"message": "NotAuthenticated"}})
            if cuerpo.get("trustedDeviceToken") in self.confianza_valida:
                return self._ok(self._entrada(), ticket=False)
            parcial = self._nuevo("parcial")
            self.parciales.add(parcial)
            return httpx.Response(200, json={"status": 4, "data": {
                "step": {"type": "verifyTwoFactor", "componentName": "TwoFactor"},
                "user": {"id": ID_USUARIO}, "authTicket": {"token": parcial, "expires": 0}}})
        if ruta == "/auth/continue/2fa/sendcode":
            if bearer not in self.parciales:
                return httpx.Response(401)
            self.sms_enviados += 1
            return httpx.Response(200, json={"status": 0, "data": {"primaryValue": "+34600000123"}})
        if ruta == "/auth/continue/2fa/result":
            if bearer not in self.parciales:
                return httpx.Response(401)
            if cuerpo.get("code") != self.codigo:
                return httpx.Response(200, json={"status": 4, "error": {"message": "InvalidCode"}})
            self.parciales.discard(bearer)
            confianza = self._nuevo("confianza") if cuerpo.get("rememberDevice") else None
            if confianza:
                self.confianza_valida.add(confianza)
            return self._ok(self._entrada(confianza), ticket=False)

        if bearer not in self.tokens:
            return httpx.Response(401)
        if ruta == "/user":
            return self._ok({"user": {"id": ID_USUARIO, "firstName": "Sintético", "lastName": "Prueba",
                                      "country": "ES", "uiLanguage": "es", "communicationLanguage": "es",
                                      "uom": "1", "dateFormat": "2", "timeFormat": "2",
                                      "dateOfBirth": 0}})
        if ruta == "/reportSettings":
            return self._ok({"settings": {"lowThreshold": 70, "highThreshold": 180,
                                          "targetRange": {"min": 70, "max": 180}},
                             "dataSources": {
                                 "a": {"id": "sensor-1", "type": 40066, "sn": "SENSOR1", "uploadDate": 100},
                                 "b": {"id": "pluma-1", "type": 90002, "sn": SERIE_PLUMA,
                                       "uploadDate": 1_763_550_000}}})
        if ruta == "/reports":
            if self.fallar_informe:
                return httpx.Response(500)
            assert cuerpo["ConnectedInsulinDeviceIds"] == ["pluma-1"]
            assert cuerpo["ReportIds"] == [540068]
            self.informes += 1
            self._sondeos = 0
            return self._ok({"url": "https://api-eu.libreview.io/poll/channel/c1"})
        if ruta == "/poll/channel/c1":
            return self._ok({"lp": "https://api-eu.libreview.io/poll/p1", "ws": "wss://x"})
        if ruta == "/poll/p1":
            self._sondeos += 1
            if self._sondeos <= self.sondeos_503:
                return httpx.Response(503)
            return httpx.Response(200, json={"operation": "report", "args": {
                "urls": {"5": "https://lrs-eu.libreview.io/report/540068/r1"}}})
        return httpx.Response(404)
