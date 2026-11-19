"""Registro tipo chat (fase 3). Todo exige sesión.

La API guarda la frase; el trabajador de comidas la interpreta con un modelo local. Daniel ve el
evento y lo confirma (tal cual o corregido) o lo descarta. Estos eventos nunca entran en un cálculo
de dosis (regla 1). Sin aviso por alcohol (Daniel, 19-11-2026).
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from api.deps import requiere_sesion

router = APIRouter(prefix="/api/chat", tags=["chat"])
SESION = Depends(requiere_sesion, scope="function")
TIPOS = ("comida", "bebida", "alcohol", "ejercicio", "enfermedad", "estres", "sueno", "otro")


def _con_zona(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is None:
        raise ValueError("la hora debe llevar zona horaria")
    return v


class Mensaje(BaseModel):
    model_config = {"extra": "forbid"}
    id_cliente: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    texto: str = Field(min_length=2, max_length=1000)


class Confirmacion(BaseModel):
    """Sin campos = confirmar tal cual. Con campos = corregido."""
    model_config = {"extra": "forbid"}
    tipo: str | None = None
    ts_evento_inicio: datetime | None = None
    ts_evento_fin: datetime | None = None
    _zona = field_validator("ts_evento_inicio", "ts_evento_fin")(_con_zona)

    @model_validator(mode="after")
    def _coherente(self):
        if self.tipo is not None and self.tipo not in TIPOS:
            raise ValueError("tipo no válido")
        if self.ts_evento_inicio and self.ts_evento_fin and self.ts_evento_fin < self.ts_evento_inicio:
            raise ValueError("el final no puede ser antes del inicio")
        return self


def para_la_app(f: dict) -> dict:
    iso = (lambda t: t.isoformat() if t else None)
    return {"id": f["id"], "ts_mensaje": iso(f["ts_mensaje"]), "texto": f["texto"], "estado": f["estado"],
            "analisis": f["estado_analisis"], "error": f["error_analisis"], "tipo": f["tipo"],
            "inicio": iso(f["ts_evento_inicio"]), "fin": iso(f["ts_evento_fin"]),
            "estimacion": f["estimacion"] or {}, "incertidumbre": f["incertidumbre"],
            "etiquetas": list(f["etiquetas"] or []), "resumen": f["resumen"], "corregido": f["corregido"]}


@router.post("")
def nuevo(m: Mensaje, con=SESION):
    f = con.execute("SELECT * FROM eventos_chat WHERE id_cliente = %s", (m.id_cliente,)).fetchone()
    if f is None:
        f = con.execute("INSERT INTO eventos_chat (texto, id_cliente) VALUES (%s, %s) RETURNING *",
                        (m.texto.strip(), m.id_cliente)).fetchone()
    return para_la_app(f)


@router.get("")
def lista(horas: int = Query(48, ge=1, le=24 * 31), con=SESION):
    filas = con.execute("""SELECT * FROM eventos_chat WHERE ts_mensaje > %s AND estado <> 'descartado'
                           ORDER BY ts_mensaje DESC LIMIT 100""",
                        (datetime.now(timezone.utc) - timedelta(hours=horas),)).fetchall()
    return {"eventos": [para_la_app(f) for f in filas]}


@router.get("/{id_}")
def ver(id_: int, con=SESION):
    f = con.execute("SELECT * FROM eventos_chat WHERE id = %s", (id_,)).fetchone()
    if f is None:
        raise HTTPException(404, "No existe ese mensaje")
    return para_la_app(f)


def _pendiente(con, id_: int) -> dict:
    f = con.execute("SELECT * FROM eventos_chat WHERE id = %s FOR UPDATE", (id_,)).fetchone()
    if f is None:
        raise HTTPException(404, "No existe ese mensaje")
    if f["estado"] != "pendiente":
        raise HTTPException(409, "Ese mensaje ya está decidido")
    return f


@router.post("/{id_}/confirmar")
def confirmar(id_: int, c: Confirmacion, con=SESION):
    f = _pendiente(con, id_)
    cambios = c.model_dump(exclude_none=True)
    tipo = cambios.get("tipo", f["tipo"])
    if tipo is None:
        raise HTTPException(422, "Elige el tipo de evento")
    inicio = cambios.get("ts_evento_inicio", f["ts_evento_inicio"])
    fin = cambios.get("ts_evento_fin", f["ts_evento_fin"])
    if inicio and fin and fin < inicio:
        fin = None                       # al mover el inicio, una duración vieja ya no vale
    etiquetas = list(f["etiquetas"] or [])
    if tipo != f["tipo"] and tipo not in ("otro", "comida", "bebida") and tipo not in etiquetas:
        etiquetas.insert(0, tipo)
    corregido = any(cambios.get(k, f[k]) != f[k] for k in ("tipo", "ts_evento_inicio", "ts_evento_fin"))
    f = con.execute(
        """UPDATE eventos_chat SET tipo = %s, ts_evento_inicio = %s, ts_evento_fin = %s, etiquetas = %s,
             estado = 'confirmado', decidido_en = now(), corregido = %s
           WHERE id = %s RETURNING *""", (tipo, inicio, fin, etiquetas, corregido, id_)).fetchone()
    return para_la_app(f)


@router.post("/{id_}/descartar")
def descartar(id_: int, con=SESION):
    _pendiente(con, id_)
    f = con.execute("""UPDATE eventos_chat SET estado = 'descartado', decidido_en = now()
                       WHERE id = %s RETURNING *""", (id_,)).fetchone()
    return para_la_app(f)
