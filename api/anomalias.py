"""Anomalías (fase 4): lo que pregunta el servicio de anomalías y las respuestas de Daniel.

La API solo lee y guarda respuestas; detectar y avisar lo hace servicios/anomalias (se comunican
por la base, regla 3). Las respuestas son etiquetas: no cambian ningún cálculo de dosis.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from api.deps import requiere_sesion

router = APIRouter(prefix="/api", tags=["anomalias"])
SESION = Depends(requiere_sesion, scope="function")
TIPOS = ("hueco", "presion", "subida_sin_comida", "dosis_sin_comida", "acumulacion", "olvido_tresiba")


class Respuesta(BaseModel):
    model_config = {"extra": "forbid"}
    respuesta: str = Field(min_length=1, max_length=40)
    nota: str | None = Field(None, max_length=500)


class Episodio(BaseModel):
    model_config = {"extra": "forbid"}
    tipo: str
    inicio: datetime
    fin: datetime
    notas: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _coherente(self):
        if self.tipo not in TIPOS:
            raise ValueError("tipo no válido")
        if self.inicio.tzinfo is None or self.fin.tzinfo is None:
            raise ValueError("la hora debe llevar zona horaria")
        if self.fin < self.inicio:
            raise ValueError("el final no puede ser antes del inicio")
        if self.fin - self.inicio > timedelta(days=2):
            raise ValueError("un episodio dura como mucho 2 días")
        return self


def para_la_app(f: dict) -> dict:
    iso = (lambda t: t.isoformat() if t else None)
    return {"id": f["id"], "tipo": f["tipo"], "inicio": iso(f["inicio"]), "fin": iso(f["fin"]),
            "urgente": f["urgente"], "titulo": f["titulo"], "texto": f["texto"], "pregunta": f["pregunta"],
            "opciones": [{"clave": o["clave"], "texto": o["texto"]} for o in f["opciones"]],
            "estado": f["estado"], "respuesta": f["respuesta"], "nota": f["nota"],
            "respondida_en": iso(f["respondida_en"]), "detectada_en": iso(f["detectada_en"]),
            "aviso": f["aviso_estado"], "avisado_en": iso(f["avisado_en"])}


def abiertas(con) -> int:
    return con.execute("SELECT count(*) AS n FROM anomalias WHERE estado = 'abierta'").fetchone()["n"]


@router.get("/anomalias")
def listar(dias: int = Query(7, ge=1, le=90), con=SESION):
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    filas = con.execute("""SELECT * FROM anomalias WHERE estado = 'abierta' OR detectada_en >= %s
                           ORDER BY detectada_en DESC LIMIT 200""", (desde,)).fetchall()
    e = con.execute("SELECT * FROM estado_anomalias").fetchone()
    return {"abiertas": [para_la_app(f) for f in filas if f["estado"] == "abierta"],
            "recientes": [para_la_app(f) for f in filas if f["estado"] != "abierta"],
            "servicio": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in e.items() if k != "unica"}
            if e else None}


@router.post("/anomalias/{id_}/respuesta")
def responder(id_: int, r: Respuesta, con=SESION):
    f = con.execute("SELECT * FROM anomalias WHERE id = %s FOR UPDATE", (id_,)).fetchone()
    if f is None:
        raise HTTPException(404, "No existe esa anomalía")
    if r.respuesta not in {o["clave"] for o in f["opciones"]}:
        raise HTTPException(422, "Esa respuesta no es una de las opciones")
    f = con.execute("""UPDATE anomalias SET estado = 'respondida', respuesta = %s, nota = %s, respondida_en = now(),
                       actualizada_en = now() WHERE id = %s RETURNING *""", (r.respuesta, r.nota, id_)).fetchone()
    return para_la_app(f)


@router.post("/anomalias/{id_}/descartar")
def descartar(id_: int, con=SESION):
    f = con.execute("""UPDATE anomalias SET estado = 'descartada', respuesta = NULL, nota = NULL, respondida_en = now(),
                       actualizada_en = now() WHERE id = %s RETURNING *""", (id_,)).fetchone()
    if f is None:
        raise HTTPException(404, "No existe esa anomalía")
    return para_la_app(f)


def _episodio_json(f: dict) -> dict:
    return {"id": f["id"], "tipo": f["tipo"], "inicio": f["inicio"].isoformat(), "fin": f["fin"].isoformat(),
            "notas": f["notas"]}


@router.get("/episodios")
def listar_episodios(con=SESION):
    return {"episodios": [_episodio_json(f) for f in
                          con.execute("SELECT * FROM episodios_etiquetados ORDER BY inicio DESC").fetchall()]}


@router.post("/episodios")
def nuevo_episodio(e: Episodio, con=SESION):
    if e.inicio > datetime.now(timezone.utc):
        raise HTTPException(422, "El episodio no puede empezar en el futuro")
    f = con.execute("""INSERT INTO episodios_etiquetados (tipo, inicio, fin, notas) VALUES (%s, %s, %s, %s)
                       RETURNING *""", (e.tipo, e.inicio, e.fin, e.notas)).fetchone()
    return _episodio_json(f)


@router.post("/episodios/{id_}/borrar")
def borrar_episodio(id_: int, con=SESION):
    if con.execute("DELETE FROM episodios_etiquetados WHERE id = %s RETURNING id", (id_,)).fetchone() is None:
        raise HTTPException(404, "No existe ese episodio")
    return {"ok": True}
