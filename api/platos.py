"""Mis platos (fase 3): ver y borrar los platos guardados al confirmar comidas. Todo exige sesión.

Se crean y actualizan solos al revisar una comida (api/comidas.py → comun.platos). Borrar un plato
solo hace que la próxima vez se vuelva a estimar con los modelos.
"""
from fastapi import APIRouter, Depends, HTTPException

from api.deps import requiere_sesion
from comun import platos as P

router = APIRouter(prefix="/api/platos", tags=["platos"])
SESION = Depends(requiere_sesion, scope="function")


def _app(p: dict) -> dict:
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    return {"id": p["id"], "nombre": p["nombre"], "hidratos_g": num(p["hidratos_g"]), "grasa_g": num(p["grasa_g"]),
            "proteina_g": num(p["proteina_g"]), "referencia": p["referencia"], "veces": p["veces"],
            "actualizado_en": p["actualizado_en"].isoformat()}


@router.get("")
def listar(con=SESION):
    return {"platos": [_app(p) for p in P.todos(con)]}


@router.post("/{id_}/borrar")
def borrar(id_: int, con=SESION):
    if con.execute("DELETE FROM platos WHERE id = %s RETURNING id", (id_,)).fetchone() is None:
        raise HTTPException(404, "No existe ese plato")
    return {"ok": True}
