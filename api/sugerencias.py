"""Sugerencias de dosis (fase 3) y parámetros activos. Todo exige sesión.

Las cifras salen solo de comun/sugerencia.py (determinista). Esta API no acepta unidades de
insulina como entrada: solo gramos, la comida revisada y el resultado de la lectura de la pluma.
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.deps import requiere_sesion
from comun import parametros as P
from comun import sugerir as SG

router = APIRouter(prefix="/api", tags=["sugerencias"])
SESION = Depends(requiere_sesion, scope="function")


def _con_zona(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is None:
        raise ValueError("la hora debe llevar zona horaria")
    return v


class PeticionSugerencia(BaseModel):
    model_config = {"extra": "forbid"}   # nunca se aceptan unidades de insulina desde fuera
    tipo: Literal["comida", "correccion"] = "comida"
    comida_id: int | None = None
    hidratos_g: float = Field(default=0, ge=0, le=500)
    hidratos_min_g: float | None = Field(default=None, ge=0, le=500)
    hidratos_max_g: float | None = Field(default=None, ge=0, le=500)
    grasa_g: float = Field(default=0, ge=0, le=300)
    proteina_g: float = Field(default=0, ge=0, le=300)
    ts_comida: datetime | None = None
    solicitud_lectura_id: int | None = None
    dosis_apuntadas: bool = False        # "no se pudo leer la pluma, pero todas mis dosis están apuntadas"
    _zona = field_validator("ts_comida")(_con_zona)


class PeticionSegunda(BaseModel):
    model_config = {"extra": "forbid"}
    solicitud_lectura_id: int | None = None
    dosis_apuntadas: bool = False


def _errores(f):
    try:
        return f()
    except SG.LecturaEnCurso as e:
        raise HTTPException(409, str(e)) from None
    except SG.PeticionNoValida as e:
        raise HTTPException(422, str(e)) from None


@router.post("/sugerencias")
def crear(p: PeticionSugerencia, con=SESION):
    f = _errores(lambda: SG.sugerir(con, **p.model_dump()))
    return SG.para_la_app(f)


@router.get("/sugerencias/{id_}")
def ver(id_: int, con=SESION):
    f = con.execute("SELECT * FROM sugerencias WHERE id = %s", (id_,)).fetchone()
    if f is None:
        raise HTTPException(404, "No existe esa sugerencia")
    return SG.para_la_app(f)


@router.post("/sugerencias/{id_}/segunda")
def segunda(id_: int, p: PeticionSegunda, con=SESION):
    f = _errores(lambda: SG.segunda(con, id_, **p.model_dump()))
    return SG.para_la_app(f)


@router.get("/parametros")
def parametros(con=SESION):
    return {"activa": P.para_la_app(P.activa(con)),
            "propuestas": [P.para_la_app(v) for v in con.execute(
                "SELECT * FROM versiones_parametros WHERE estado = 'propuesta' ORDER BY version").fetchall()]}
