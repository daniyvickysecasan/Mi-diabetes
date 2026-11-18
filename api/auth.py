"""Autenticación con passkey (WebAuthn). Un único usuario.

Alta de la primera passkey: exige CODIGO_ALTA (variable de entorno). Para añadir más passkeys
hace falta además una sesión activa, así que el código por sí solo no basta una vez dada de alta
la primera. Las passkeys son "discoverable": para entrar no hace falta escribir usuario.
"""
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from api.deps import BD, COOKIE, hash_token, sesion_actual

router = APIRouter(prefix="/auth", tags=["autenticación"])
ID_USUARIO = b"mi-diabetes-usuario-unico"
VIDA_RETO = timedelta(minutes=5)


class PeticionAlta(BaseModel):
    codigo_alta: str


class Verificacion(BaseModel):
    id_reto: str
    credencial: dict
    nombre: str | None = None


def _ahora():
    return datetime.now(timezone.utc)


def _guardar_reto(con, reto: bytes, tipo: str) -> str:
    con.execute("DELETE FROM retos_webauthn WHERE expira_en < %s", (_ahora(),))
    id_reto = secrets.token_urlsafe(16)
    con.execute("INSERT INTO retos_webauthn (id, reto, tipo, expira_en) VALUES (%s, %s, %s, %s)",
                (id_reto, reto, tipo, _ahora() + VIDA_RETO))
    return id_reto


def _consumir_reto(con, id_reto: str, tipo: str) -> bytes:
    fila = con.execute("DELETE FROM retos_webauthn WHERE id = %s AND tipo = %s AND expira_en > %s RETURNING reto",
                       (id_reto, tipo, _ahora())).fetchone()
    if not fila:
        raise HTTPException(400, "Reto caducado o desconocido; vuelve a intentarlo")
    return bytes(fila["reto"])


def _abrir_sesion(request: Request, response: Response, con, id_credencial: bytes) -> None:
    aj = request.app.state.ajustes
    token = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sesiones (hash_token, id_credencial, expira_en) VALUES (%s, %s, %s)",
                (hash_token(token), id_credencial, _ahora() + timedelta(days=aj.dias_sesion)))
    response.set_cookie(COOKIE, token, max_age=aj.dias_sesion * 86400, httponly=True,
                        secure=aj.cookie_segura, samesite="strict", path="/")


@router.get("/estado")
def estado(request: Request, con=BD):
    hay = con.execute("SELECT count(*) AS n FROM credenciales_passkey").fetchone()["n"] > 0
    return {"autenticado": sesion_actual(request, con) is not None, "hay_passkeys": hay}


@router.post("/alta/opciones")
def alta_opciones(datos: PeticionAlta, request: Request, con=BD):
    aj = request.app.state.ajustes
    if not aj.codigo_alta:
        raise HTTPException(403, "Alta de passkeys desactivada: define CODIGO_ALTA")
    if not hmac.compare_digest(datos.codigo_alta.encode(), aj.codigo_alta.encode()):
        raise HTTPException(403, "Código de alta incorrecto")
    existentes = con.execute("SELECT id_credencial FROM credenciales_passkey").fetchall()
    if existentes and sesion_actual(request, con) is None:
        raise HTTPException(403, "Ya hay una passkey: entra con ella para añadir otra")
    opciones = generate_registration_options(
        rp_id=aj.rp_id, rp_name=aj.rp_nombre, user_id=ID_USUARIO, user_name="yo",
        user_display_name="Mi Diabetes",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=bytes(f["id_credencial"])) for f in existentes])
    return {"id_reto": _guardar_reto(con, opciones.challenge, "alta"),
            "opciones": json.loads(options_to_json(opciones))}


@router.post("/alta/verificar")
def alta_verificar(datos: Verificacion, request: Request, response: Response, con=BD):
    aj = request.app.state.ajustes
    reto = _consumir_reto(con, datos.id_reto, "alta")
    try:
        v = verify_registration_response(credential=datos.credencial, expected_challenge=reto,
                                         expected_rp_id=aj.rp_id, expected_origin=list(aj.origenes),
                                         require_user_verification=True)
    except Exception as e:
        raise HTTPException(400, f"Passkey no válida: {e}") from None
    transportes = datos.credencial.get("response", {}).get("transports") or []
    con.execute("""INSERT INTO credenciales_passkey (id_credencial, clave_publica, contador, transportes, nombre)
                   VALUES (%s, %s, %s, %s, %s)""",
                (v.credential_id, v.credential_public_key, v.sign_count, transportes,
                 (datos.nombre or "passkey")[:60]))
    if sesion_actual(request, con) is None:
        _abrir_sesion(request, response, con, v.credential_id)
    return {"ok": True}


@router.post("/entrada/opciones")
def entrada_opciones(request: Request, con=BD):
    aj = request.app.state.ajustes
    opciones = generate_authentication_options(rp_id=aj.rp_id,
                                               user_verification=UserVerificationRequirement.REQUIRED)
    return {"id_reto": _guardar_reto(con, opciones.challenge, "entrada"),
            "opciones": json.loads(options_to_json(opciones))}


@router.post("/entrada/verificar")
def entrada_verificar(datos: Verificacion, request: Request, response: Response, con=BD):
    aj = request.app.state.ajustes
    reto = _consumir_reto(con, datos.id_reto, "entrada")
    try:
        id_cred = base64url_to_bytes(datos.credencial.get("rawId") or datos.credencial.get("id") or "")
    except Exception:
        raise HTTPException(400, "Credencial mal formada") from None
    fila = con.execute("SELECT * FROM credenciales_passkey WHERE id_credencial = %s", (id_cred,)).fetchone()
    if not fila:
        raise HTTPException(401, "Passkey desconocida")
    try:
        v = verify_authentication_response(credential=datos.credencial, expected_challenge=reto,
                                           expected_rp_id=aj.rp_id, expected_origin=list(aj.origenes),
                                           credential_public_key=bytes(fila["clave_publica"]),
                                           credential_current_sign_count=fila["contador"],
                                           require_user_verification=True)
    except Exception as e:
        raise HTTPException(401, f"No se pudo verificar la passkey: {e}") from None
    con.execute("UPDATE credenciales_passkey SET contador = %s, ultimo_uso = now() WHERE id_credencial = %s",
                (v.new_sign_count, id_cred))
    _abrir_sesion(request, response, con, id_cred)
    return {"ok": True}


@router.post("/salir")
def salir(request: Request, response: Response, con=BD):
    token = request.cookies.get(COOKIE)
    if token:
        con.execute("DELETE FROM sesiones WHERE hash_token = %s", (hash_token(token),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
