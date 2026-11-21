"""Web Push a la PWA (fase 4): API de suscripciones, envío con ntfy solo para lo urgente y claves VAPID.
Nunca se llama a un servicio de push real."""
import base64
import dataclasses
import os
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pywebpush import WebPushException, webpush

from api.main import crear_app
from servicios.anomalias import __main__ as CLI
from servicios.anomalias import avisos as A
from servicios.anomalias import detector as D
from servicios.anomalias import reglas as R
from tests.autenticador_software import Autenticador
from tests.test_anomalias import AHORA, _dosis, _lecturas, _parametros, ntfy  # noqa: F401  (fixture)
from tests.test_api import ORIGEN, _alta

U = R.Umbrales()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _claves_navegador() -> dict:
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"p256dh": _b64(pub), "auth": _b64(os.urandom(16))}


def _suscribir(con, endpoint="https://web.push.apple.com/abc", **k):
    c = _claves_navegador()
    con.execute("INSERT INTO suscripciones_push (endpoint, p256dh, auth, nombre) VALUES (%s, %s, %s, 'iPhone')",
                (endpoint, c["p256dh"], c["auth"]))


class Respuesta:
    def __init__(self, codigo):
        self.status_code = codigo


class PushFalso:
    def __init__(self, fallo=None):
        self.enviados, self.fallo = [], fallo

    def __call__(self, **k):
        if self.fallo:
            raise WebPushException("fallo", response=Respuesta(self.fallo))
        self.enviados.append(k)


def _cliente(falso):
    return A.ClienteWebPush("clave", "https://app.test", enviar=falso)


def _hueco(con, ahora=AHORA):
    _lecturas(con, [120] * 6, hasta=ahora - timedelta(minutes=40))
    D.ciclo(con, ahora, U)


# ------------------------------------------------------------------ envío
def test_no_urgente_solo_por_webpush(con, ntfy):
    _suscribir(con)
    _hueco(con)
    falso = PushFalso()
    r = A.enviar_pendientes(con, ntfy, AHORA, U, "https://app.test", _cliente(falso), "urgentes")
    assert r["enviados"] == 1 and len(falso.enviados) == 1 and ntfy.app.state.mensajes == []
    k = falso.enviados[0]
    assert k["vapid_claims"] == {"sub": "https://app.test"} and k["headers"]["Urgency"] == "normal"
    assert '"/#avisos"' in k["data"] and "No llegan lecturas" in k["data"]
    a = con.execute("SELECT * FROM anomalias").fetchone()
    assert a["canales"] == {"webpush": 1}
    assert con.execute("SELECT ultimo_ok FROM suscripciones_push").fetchone()["ultimo_ok"] == AHORA


def test_urgente_por_los_dos(con, ntfy):
    _suscribir(con)
    _parametros(con)
    _lecturas(con, [140] * 12)
    _dosis(con, AHORA - timedelta(minutes=30), u=4)
    D.ciclo(con, AHORA, U)
    falso = PushFalso()
    A.enviar_pendientes(con, ntfy, AHORA, U, "https://app.test", _cliente(falso), "urgentes")
    a = con.execute("SELECT * FROM anomalias WHERE tipo = 'acumulacion'").fetchone()
    assert a["canales"] == {"webpush": 1, "ntfy": True}
    assert falso.enviados[0]["headers"]["Urgency"] == "high"
    assert [m["priority"] for m in ntfy.app.state.mensajes] == [5]


def test_sin_suscripciones_o_si_falla_va_por_ntfy(con, ntfy):
    _hueco(con)
    A.enviar_pendientes(con, ntfy, AHORA, U, "", _cliente(PushFalso()), "urgentes")
    assert con.execute("SELECT canales FROM anomalias").fetchone()["canales"] == {"ntfy": True}

    con.execute("TRUNCATE anomalias")
    _suscribir(con)
    _hueco(con, AHORA + timedelta(hours=1))
    A.enviar_pendientes(con, ntfy, AHORA + timedelta(hours=1), U, "", _cliente(PushFalso(fallo=500)), "urgentes")
    assert con.execute("SELECT canales FROM anomalias").fetchone()["canales"] == {"ntfy": True}
    s = con.execute("SELECT * FROM suscripciones_push").fetchone()
    assert s["activa"] and s["errores"] == 1


def test_suscripcion_caducada_se_desactiva_y_sin_canales_reintenta(con):
    _suscribir(con)
    _hueco(con)
    r = A.enviar_pendientes(con, None, AHORA, U, "", _cliente(PushFalso(fallo=410)), "urgentes")
    assert r["enviados"] == 0
    assert con.execute("SELECT activa FROM suscripciones_push").fetchone()["activa"] is False
    a = con.execute("SELECT * FROM anomalias").fetchone()
    assert a["aviso_estado"] == "pendiente"                 # se queda en la app hasta que caduque

    _suscribir(con, "https://web.push.apple.com/otra")
    r = A.enviar_pendientes(con, None, AHORA, U, "", _cliente(PushFalso(fallo=500)), "urgentes")
    a = con.execute("SELECT * FROM anomalias").fetchone()
    assert r["errores"] == 1 and a["aviso_intentos"] == 1 and a["aviso_estado"] == "pendiente"


def test_ntfy_para_todos(con, ntfy):
    _suscribir(con)
    _hueco(con)
    A.enviar_pendientes(con, ntfy, AHORA, U, "", _cliente(PushFalso()), "todos")
    assert con.execute("SELECT canales FROM anomalias").fetchone()["canales"] == {"webpush": 1, "ntfy": True}


def test_claves_vapid_firman_y_cifran_de_verdad(tmp_path, monkeypatch):
    """Las claves que genera claves-push sirven a pywebpush (curl=True: sin red; deja encrypted.data)."""
    monkeypatch.chdir(tmp_path)
    c = CLI.claves_push()
    assert len(c["VAPID_PUBLICA"]) == 87
    sub = {"endpoint": "https://web.push.apple.com/xyz", "p256dh": "", "auth": ""} | _claves_navegador()
    salida = []
    cli = A.ClienteWebPush(c["VAPID_PRIVADA"], "https://app.test",
                           enviar=lambda **k: salida.append(webpush(**k, curl=True)))
    cli.publicar(sub, {"titulo": "x", "texto": "y"})
    assert "web.push.apple.com" in salida[0] and "vapid t=" in salida[0].lower()


# ------------------------------------------------------------------ API
@pytest.fixture
def cli(con, ajustes):
    aj = dataclasses.replace(ajustes, vapid_publica="BPublicaDePrueba")
    c = TestClient(crear_app(aj), base_url=ORIGEN)
    assert _alta(c, Autenticador(ajustes.rp_id, ORIGEN)).status_code == 200
    return c


def test_api_push_exige_sesion(con, ajustes):
    c = TestClient(crear_app(ajustes), base_url=ORIGEN)
    assert c.get("/api/push/clave").status_code == 401
    assert c.post("/api/push/suscripcion", json={}).status_code == 401


def test_api_suscribir_y_baja(cli, con):
    assert cli.get("/api/push/clave").json() == {"clave": "BPublicaDePrueba"}
    k = _claves_navegador()
    cuerpo = {"endpoint": "https://web.push.apple.com/abc", "expirationTime": None, "keys": k, "nombre": "iPhone"}
    assert cli.post("/api/push/suscripcion", json=cuerpo).json()["servicio"] == "web.push.apple.com"
    assert cli.post("/api/push/suscripcion", json=cuerpo).status_code == 200            # repetir no duplica
    assert len(cli.get("/api/push").json()["suscripciones"]) == 1
    assert cli.post("/api/push/suscripcion", json=cuerpo | {"endpoint": "http://malo.test/x"}).status_code == 422
    assert cli.post("/api/push/baja", json={"endpoint": cuerpo["endpoint"]}).json() == {"ok": True}
    assert cli.get("/api/push").json()["suscripciones"][0]["activa"] is False
    assert cli.post("/api/push/suscripcion", json=cuerpo).json()["activa"] is True       # volver a activar
