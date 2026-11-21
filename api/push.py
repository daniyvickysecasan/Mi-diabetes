"""Suscripciones Web Push de la PWA (fase 4). Todo exige sesión.

La PWA pide permiso, se suscribe con la clave pública VAPID y deja aquí su suscripción; el servicio
de anomalías envía los avisos (se comunican por la base, regla 3).
"""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from api.deps import requiere_sesion

router = APIRouter(prefix="/api/push", tags=["push"])
SESION = Depends(requiere_sesion, scope="function")


class Claves(BaseModel):
    model_config = {"extra": "ignore"}
    p256dh: str = Field(min_length=20, max_length=200, pattern=r"^[A-Za-z0-9_=-]+$")
    auth: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_=-]+$")


class Suscripcion(BaseModel):
    model_config = {"extra": "ignore"}       # el navegador añade expirationTime
    endpoint: str = Field(max_length=1000)
    keys: Claves
    nombre: str | None = Field(None, max_length=60)

    @field_validator("endpoint")
    @classmethod
    def _https(cls, v):
        u = urlparse(v)
        if u.scheme != "https" or not u.hostname:
            raise ValueError("el endpoint debe ser https")
        return v


class Baja(BaseModel):
    endpoint: str = Field(max_length=1000)


def _json(f: dict) -> dict:
    iso = (lambda t: t.isoformat() if t else None)
    return {"id": f["id"], "nombre": f["nombre"], "servicio": urlparse(f["endpoint"]).hostname,
            "activa": f["activa"], "creado_en": iso(f["creado_en"]), "ultimo_ok": iso(f["ultimo_ok"]),
            "ultimo_error": f["ultimo_error"]}


@router.get("/clave")
def clave(request: Request, con=SESION):
    return {"clave": request.app.state.ajustes.vapid_publica or None}


@router.get("")
def listar(con=SESION):
    return {"suscripciones": [_json(f) for f in con.execute("SELECT * FROM suscripciones_push ORDER BY id").fetchall()]}


@router.post("/suscripcion")
def suscribir(s: Suscripcion, con=SESION):
    f = con.execute(
        """INSERT INTO suscripciones_push (endpoint, p256dh, auth, nombre) VALUES (%s, %s, %s, %s)
           ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth,
             nombre = coalesce(EXCLUDED.nombre, suscripciones_push.nombre), activa = true, errores = 0,
             ultimo_error = NULL
           RETURNING *""", (s.endpoint, s.keys.p256dh, s.keys.auth, s.nombre)).fetchone()
    return _json(f)


@router.post("/baja")
def baja(b: Baja, con=SESION):
    f = con.execute("UPDATE suscripciones_push SET activa = false WHERE endpoint = %s RETURNING *",
                    (b.endpoint,)).fetchone()
    return {"ok": f is not None}
