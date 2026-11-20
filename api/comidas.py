"""Comidas por foto o texto (fase 3). Todo exige sesión.

La API no llama a los modelos (regla 3): guarda la foto en el volumen local y deja la comida
'pendiente'; el trabajador de comidas la analiza y la deja 'lista'. Los gramos estimados solo
entran en una dosis cuando Daniel revisa la comida (POST /revisar): un toque en "Correcto" o
corrigiendo algún valor.
"""
import base64
import binascii
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api.deps import requiere_sesion

router = APIRouter(prefix="/api/comidas", tags=["comidas"])
SESION = Depends(requiere_sesion, scope="function")
MAX_FOTO = 6 * 1024 * 1024
FIRMAS = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}


def _con_zona(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is None:
        raise ValueError("la hora debe llevar zona horaria")
    return v


class NuevaFoto(BaseModel):
    model_config = {"extra": "forbid"}
    id_cliente: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    imagen_b64: str = Field(min_length=100, max_length=MAX_FOTO * 4 // 3 + 16)
    nota: str | None = Field(default=None, max_length=1000)
    ts: datetime | None = None
    _zona = field_validator("ts")(_con_zona)


class NuevoTexto(BaseModel):
    model_config = {"extra": "forbid"}
    id_cliente: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    texto: str = Field(min_length=3, max_length=1000)
    ts: datetime | None = None
    _zona = field_validator("ts")(_con_zona)


class Revision(BaseModel):
    """Lo que Daniel ve y confirma. Sin valores = "Correcto" tal cual."""
    model_config = {"extra": "forbid"}
    hidratos_g: float | None = Field(default=None, ge=0, le=500)
    grasa_g: float | None = Field(default=None, ge=0, le=300)
    proteina_g: float | None = Field(default=None, ge=0, le=300)


def _num(x):
    return float(x) if x is not None else None


def para_la_app(f: dict) -> dict:
    return {"id": f["id"], "ts": f["ts"].isoformat(), "origen": f["origen"], "descripcion": f["descripcion"],
            "estado": f["estado_analisis"], "error": f["error_analisis"], "revisada": f["revisada"],
            "corregida": f["corregida"], "hidratos_g": _num(f["hidratos_bolo_g"]),
            "hidratos_min_g": _num(f["rango_hidratos_min_g"]), "hidratos_max_g": _num(f["rango_hidratos_max_g"]),
            "grasa_g": _num(f["grasa_g"]), "proteina_g": _num(f["proteina_g"]), "kcal": _num(f["kcal"]),
            "items": f["items"] or [], "alertas": f["alertas"] or [],
            "segundos": (f["estimaciones"] or {}).get("segundos")}


def _hora(ts: datetime | None) -> datetime:
    ahora = datetime.now(timezone.utc)
    if ts is None:
        return ahora
    if ts > ahora + timedelta(minutes=10):
        raise HTTPException(422, "La hora está en el futuro")
    return ts


def _existente(con, id_cliente: str):
    return con.execute("SELECT * FROM comidas WHERE id_cliente = %s", (id_cliente,)).fetchone()


@router.post("/foto")
def nueva_foto(p: NuevaFoto, request: Request, con=SESION):
    if (f := _existente(con, p.id_cliente)) is not None:          # reenvío: no duplica
        return para_la_app(f)
    try:
        datos = base64.b64decode(p.imagen_b64.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "La imagen no es válida") from None
    ext = next((e for firma, e in FIRMAS.items() if datos.startswith(firma)), None)
    if ext is None or len(datos) > MAX_FOTO:
        raise HTTPException(422, "La foto debe ser JPEG o PNG de menos de 6 MB")
    carpeta = Path(request.app.state.ajustes.ruta_fotos)
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = f"{uuid.uuid4().hex}{ext}"
    (carpeta / nombre).write_bytes(datos)
    f = con.execute(
        """INSERT INTO comidas (ts, origen, descripcion, foto_ruta, estado_analisis, id_cliente)
           VALUES (%s, 'foto', %s, %s, 'pendiente', %s) RETURNING *""",
        (_hora(p.ts), p.nota, nombre, p.id_cliente)).fetchone()
    return para_la_app(f)


@router.post("/texto")
def nuevo_texto(p: NuevoTexto, con=SESION):
    if (f := _existente(con, p.id_cliente)) is not None:
        return para_la_app(f)
    f = con.execute(
        """INSERT INTO comidas (ts, origen, descripcion, estado_analisis, id_cliente)
           VALUES (%s, 'texto', %s, 'pendiente', %s) RETURNING *""",
        (_hora(p.ts), p.texto.strip(), p.id_cliente)).fetchone()
    return para_la_app(f)


@router.get("/{id_}")
def ver(id_: int, con=SESION):
    f = con.execute("SELECT * FROM comidas WHERE id = %s", (id_,)).fetchone()
    if f is None:
        raise HTTPException(404, "No existe esa comida")
    return para_la_app(f)


@router.post("/{id_}/revisar")
def revisar(id_: int, r: Revision, con=SESION):
    f = con.execute("SELECT * FROM comidas WHERE id = %s FOR UPDATE", (id_,)).fetchone()
    if f is None:
        raise HTTPException(404, "No existe esa comida")
    if f["revisada"]:
        return para_la_app(f)
    if f["estado_analisis"] != "lista" and r.hidratos_g is None:
        raise HTTPException(422, "La comida aún no tiene estimación: escribe los hidratos")
    hc = r.hidratos_g if r.hidratos_g is not None else _num(f["hidratos_bolo_g"])
    lo, hi = _num(f["rango_hidratos_min_g"]), _num(f["rango_hidratos_max_g"])
    corregida = any(v is not None and v != _num(f[k]) for v, k in
                    ((r.hidratos_g, "hidratos_bolo_g"), (r.grasa_g, "grasa_g"), (r.proteina_g, "proteina_g")))
    if r.hidratos_g is not None and r.hidratos_g != _num(f["hidratos_bolo_g"]):
        lo, hi = None, None              # Daniel ha puesto los gramos: el rango de los modelos ya no vale
    f = con.execute(
        """UPDATE comidas SET hidratos_bolo_g = %s, rango_hidratos_min_g = %s, rango_hidratos_max_g = %s,
             grasa_g = coalesce(%s, grasa_g), proteina_g = coalesce(%s, proteina_g),
             revisada = true, revisada_en = now(), corregida = %s
           WHERE id = %s RETURNING *""",
        (hc, lo, hi, r.grasa_g, r.proteina_g, corregida, id_)).fetchone()
    return para_la_app(f)
