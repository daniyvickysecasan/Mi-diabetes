"""Sugerencias de dosis (fase 3) y parámetros activos. Todo exige sesión.

Las cifras salen solo de comun/sugerencia.py (determinista). El cálculo no acepta unidades de
insulina como entrada: solo gramos, la comida revisada y el resultado de la lectura de la pluma.
Las únicas unidades que llegan de fuera son las que Daniel dice haberse puesto al marcar una
sugerencia como "modificado": es un registro, como una dosis apuntada, no entra en el cálculo.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from api.deps import requiere_sesion
from comun import decisiones as D
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


class PeticionDecision(BaseModel):
    model_config = {"extra": "forbid"}
    id_cliente: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    decision: Literal["hecho", "modificado", "ignorado"]
    unidades: float | None = Field(default=None, ge=0, lt=100)   # solo en "modificado": lo que te pusiste
    ts: datetime | None = None                                    # hora de la inyección (por defecto, ahora)
    notas: str | None = Field(default=None, max_length=500)
    _zona = field_validator("ts")(_con_zona)


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


def _con_decision(con, f: dict) -> dict:
    r = SG.para_la_app(f)
    r["decision"] = D.para_la_app(con.execute("SELECT * FROM decisiones WHERE sugerencia_id = %s",
                                              (f["id"],)).fetchone())
    r["segunda_comprobada_id"] = None
    if f["u_segunda"] is not None:
        hija = con.execute("SELECT id FROM sugerencias WHERE sugerencia_origen_id = %s ORDER BY id DESC LIMIT 1",
                           (f["id"],)).fetchone()
        r["segunda_comprobada_id"] = hija["id"] if hija else None
    return r


@router.get("/sugerencias")
def listar(horas: int = Query(24, ge=1, le=24 * 14), con=SESION):
    """Sugerencias recientes con su decisión y, pasadas 4 h, su resultado."""
    D.evaluar_pendientes(con)
    filas = con.execute("SELECT * FROM sugerencias WHERE ts >= %s ORDER BY ts DESC LIMIT 50",
                        (datetime.now(timezone.utc) - timedelta(hours=horas),)).fetchall()
    return {"sugerencias": [_con_decision(con, f) for f in filas]}


@router.get("/sugerencias/{id_}")
def ver(id_: int, con=SESION):
    f = con.execute("SELECT * FROM sugerencias WHERE id = %s", (id_,)).fetchone()
    if f is None:
        raise HTTPException(404, "No existe esa sugerencia")
    return _con_decision(con, f)


@router.post("/sugerencias/{id_}/decision")
def decidir(id_: int, p: PeticionDecision, con=SESION):
    if p.ts and p.ts > datetime.now(timezone.utc) + timedelta(minutes=10):
        raise HTTPException(422, "La hora está en el futuro")
    if p.decision != "modificado" and p.unidades is not None:
        raise HTTPException(422, "Las unidades solo se escriben si has modificado la dosis")
    try:
        r = D.decidir(con, id_, p.decision, unidades=p.unidades, ts=p.ts, notas=p.notas, id_cliente=p.id_cliente)
    except D.DecisionNoValida as e:
        raise HTTPException(422, str(e)) from None
    f = con.execute("SELECT * FROM sugerencias WHERE id = %s", (id_,)).fetchone()
    return {"nueva": r["nueva"], "sugerencia": _con_decision(con, f)}


@router.post("/sugerencias/{id_}/segunda")
def segunda(id_: int, p: PeticionSegunda, con=SESION):
    f = _errores(lambda: SG.segunda(con, id_, **p.model_dump()))
    return SG.para_la_app(f)


@router.get("/parametros")
def parametros(con=SESION):
    return {"activa": P.para_la_app(P.activa(con)),
            "propuestas": [P.para_la_app(v) for v in con.execute(
                "SELECT * FROM versiones_parametros WHERE estado = 'propuesta' ORDER BY version").fetchall()]}
