"""Conexión con LibreView desde la app: estado, pedir el SMS y escribir el código.

La API no habla con LibreView: solo deja la petición en la base (estado_libreview) y el recolector
de la pluma la atiende en su siguiente vuelta (cada INTERVALO_PLUMA_S).
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.deps import requiere_sesion
from comun import estado_libreview as E

router = APIRouter(prefix="/api/libreview", tags=["libreview"])
SESION = Depends(requiere_sesion, scope="function")


def _para_la_app(request: Request, con) -> dict:
    return E.para_la_app(con, intervalo=timedelta(seconds=request.app.state.ajustes.intervalo_informe_s))


class Codigo(BaseModel):
    codigo: str = Field(min_length=4, max_length=12)


@router.get("/estado")
def estado(request: Request, con=SESION):
    return _para_la_app(request, con)


@router.post("/sms")
def pedir_sms(request: Request, con=SESION):
    try:
        E.pedir_sms(con)
    except E.EstadoNoPermite as e:
        raise HTTPException(409, str(e)) from None
    return _para_la_app(request, con)


@router.post("/codigo")
def dar_codigo(c: Codigo, request: Request, con=SESION):
    try:
        E.dar_codigo(con, c.codigo)
    except E.EstadoNoPermite as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return _para_la_app(request, con)
