"""Dependencias comunes: conexión a la base y sesión obligatoria."""
import hashlib
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request

from comun.bd import conectar

COOKIE = "sesion"


def bd(request: Request):
    with conectar(request.app.state.ajustes.database_url) as con:
        yield con


# La transacción se confirma ANTES de enviar la respuesta (scope="function").
BD = Depends(bd, scope="function")


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def sesion_actual(request: Request, con) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    fila = con.execute(
        "UPDATE sesiones SET ultimo_uso = now() WHERE hash_token = %s AND expira_en > %s RETURNING *",
        (hash_token(token), datetime.now(timezone.utc))).fetchone()
    return fila


def requiere_sesion(request: Request, con=BD):
    s = sesion_actual(request, con)
    if s is None:
        raise HTTPException(401, "Necesitas entrar con tu passkey")
    return con
