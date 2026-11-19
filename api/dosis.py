"""Endpoints de la fase 2: dosis de Fiasp, revisión de la conciliación, hidratos de corrección,
Tresiba, insulina activa y métricas. Todos exigen sesión.

Los envíos de la PWA llevan `id_cliente` (lo genera el móvil): si se reenvían desde la cola sin
conexión, la base los reconoce y no duplica nada.
"""
from datetime import datetime, time, timedelta, timezone
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from api.deps import requiere_sesion
from comun import aviso_pluma as AP
from comun import conciliacion as C
from comun import metricas as M
from comun import tresiba as T
from comun.insulina import FIASP, insulina_activa_bd

router = APIRouter(prefix="/api", tags=["dosis"])
SESION = Depends(requiere_sesion, scope="function")
FUTURO_MAX = timedelta(minutes=10)


def _ahora():
    return datetime.now(timezone.utc)


def _con_zona(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("la hora debe llevar zona horaria")
    return v.astimezone(timezone.utc)


class NuevaDosis(BaseModel):
    id_cliente: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    ts: datetime
    unidades: float = Field(gt=0, lt=1000)
    comida_id: int | None = None
    notas: str | None = Field(default=None, max_length=500)
    _zona = field_validator("ts")(_con_zona)


class NuevosHidratos(BaseModel):
    id_cliente: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    ts: datetime
    gramos: float = Field(gt=0, le=500)
    motivo: Literal["hipoglucemia", "picoteo", "ejercicio", "otro"] = "hipoglucemia"
    notas: str | None = Field(default=None, max_length=500)
    _zona = field_validator("ts")(_con_zona)


class Revision(BaseModel):
    accion: Literal["aceptar", "separar", "descartar", "reabrir"]


class CambioComida(BaseModel):
    comida_id: int | None


class NuevaTresiba(BaseModel):
    unidades: float = Field(gt=0, lt=1000)
    hora_local: time                     # inicio de la franja
    hora_fin_local: time                 # fin de la franja: obligatorio, para que una versión vieja
                                         # de la app no guarde en silencio una franja de anchura cero
    vigente_desde: datetime | None = None
    notas: str | None = Field(default=None, max_length=500)


def _no_futura(ts: datetime):
    if ts > _ahora() + FUTURO_MAX:
        raise HTTPException(422, "La hora está en el futuro")


def _dosis_json(f: dict) -> dict:
    return {
        "id": f["id"], "ts": f["ts"].isoformat(), "unidades": float(f["unidades"]), "insulina": f["insulina"],
        "origen": f["origen"], "estado": f["estado_conciliacion"], "par_id": f["dosis_par_id"],
        "comida_id": f["comida_id"], "notas": f["notas"], "requiere_revision": f["requiere_revision"],
        "motivo": f["motivo_conciliacion"], "revision": f["revision"],
        "revisada_en": f["revisada_en"].isoformat() if f["revisada_en"] else None,
        "cuenta": f["cuenta"],
    }


SQL_DOSIS = """SELECT d.*, EXISTS (SELECT 1 FROM dosis_efectivas e WHERE e.id = d.id) AS cuenta FROM dosis d"""


# ------------------------------------------------------------------ dosis
@router.post("/dosis")
def registrar_dosis(d: NuevaDosis, con=SESION):
    _no_futura(d.ts)
    if d.comida_id is not None and not con.execute("SELECT 1 FROM comidas WHERE id = %s", (d.comida_id,)).fetchone():
        raise HTTPException(422, "Esa comida no existe")
    f = con.execute(
        """INSERT INTO dosis (ts, unidades, insulina, origen, id_externo, comida_id, notas)
           VALUES (%s, %s, 'fiasp', 'app', %s, %s, %s)
           ON CONFLICT (origen, id_externo) DO NOTHING RETURNING id""",
        (d.ts, round(d.unidades, 2), d.id_cliente, d.comida_id, d.notas)).fetchone()
    nueva = f is not None
    if nueva:
        C.conciliar(con)
    fila = con.execute(SQL_DOSIS + " WHERE d.origen = 'app' AND d.id_externo = %s", (d.id_cliente,)).fetchone()
    return {"nueva": nueva, "dosis": _dosis_json(fila)}


@router.get("/dosis")
def listar_dosis(horas: int = Query(48, ge=1, le=24 * 90), con=SESION):
    filas = con.execute(SQL_DOSIS + " WHERE d.ts >= %s ORDER BY d.ts DESC, d.id DESC",
                        (_ahora() - timedelta(hours=horas),)).fetchall()
    return {"dosis": [_dosis_json(f) for f in filas]}


@router.get("/dosis/revisar")
def por_revisar(con=SESION):
    filas = con.execute(SQL_DOSIS + " WHERE d.requiere_revision ORDER BY d.ts DESC").fetchall()
    pares = {}
    ids = [f["dosis_par_id"] for f in filas if f["dosis_par_id"]]
    if ids:
        pares = {p["id"]: _dosis_json(p) for p in con.execute(SQL_DOSIS + " WHERE d.id = ANY(%s)", (ids,)).fetchall()}
    salida, vistos = [], set()
    for f in filas:
        if f["id"] in vistos:
            continue
        vistos |= {f["id"], f["dosis_par_id"]}
        salida.append({**_dosis_json(f), "par": pares.get(f["dosis_par_id"])})
    return {"dosis": salida}


@router.post("/dosis/{dosis_id}/revision")
def revisar_dosis(dosis_id: int, r: Revision, con=SESION):
    try:
        res = C.revisar(con, dosis_id, r.accion)
    except C.RevisionInvalida as e:
        raise HTTPException(422, str(e))
    return {"conciliacion": res}


@router.post("/dosis/{dosis_id}/comida")
def cambiar_comida(dosis_id: int, c: CambioComida, con=SESION):
    """La app manda en el vínculo con la comida: se guarda en la fila de la app si la hay."""
    d = con.execute("SELECT * FROM dosis WHERE id = %s", (dosis_id,)).fetchone()
    if d is None:
        raise HTTPException(404, "No existe esa dosis")
    if c.comida_id is not None and not con.execute("SELECT 1 FROM comidas WHERE id = %s", (c.comida_id,)).fetchone():
        raise HTTPException(422, "Esa comida no existe")
    destino = dosis_id
    if d["origen"] in C.ORIGENES_PLUMA:
        app = con.execute(
            """SELECT id FROM dosis WHERE dosis_par_id = %s AND origen NOT IN ('pluma_libreview', 'csv_libreview')
               AND estado_conciliacion IN ('emparejada', 'discrepancia')""", (dosis_id,)).fetchone()
        destino = app["id"] if app else dosis_id
    con.execute("UPDATE dosis SET comida_id = %s WHERE id = %s", (c.comida_id, destino))
    return {"ok": True, "dosis_id": destino}


@router.get("/comidas/recientes")
def comidas_recientes(horas: int = Query(8, ge=1, le=72), con=SESION):
    filas = con.execute(
        "SELECT id, ts, descripcion, hidratos_g FROM comidas WHERE ts >= %s ORDER BY ts DESC LIMIT 20",
        (_ahora() - timedelta(hours=horas),)).fetchall()
    return {"comidas": [{"id": f["id"], "ts": f["ts"].isoformat(), "descripcion": f["descripcion"],
                         "hidratos_g": float(f["hidratos_g"]) if f["hidratos_g"] is not None else None}
                        for f in filas]}


# ------------------------------------------------------------------ hidratos de corrección
def _hidratos_json(f):
    return {"id": f["id"], "ts": f["ts"].isoformat(), "gramos": float(f["gramos"]), "motivo": f["motivo"],
            "notas": f["notas"]}


@router.post("/hidratos-correccion")
def registrar_hidratos(h: NuevosHidratos, con=SESION):
    _no_futura(h.ts)
    f = con.execute(
        """INSERT INTO hidratos_correccion (ts, gramos, motivo, notas, id_cliente) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (id_cliente) DO NOTHING RETURNING id""",
        (h.ts, h.gramos, h.motivo, h.notas, h.id_cliente)).fetchone()
    fila = con.execute("SELECT * FROM hidratos_correccion WHERE id_cliente = %s", (h.id_cliente,)).fetchone()
    return {"nueva": f is not None, "hidratos": _hidratos_json(fila)}


@router.get("/hidratos-correccion")
def listar_hidratos(horas: int = Query(48, ge=1, le=24 * 90), con=SESION):
    filas = con.execute("SELECT * FROM hidratos_correccion WHERE ts >= %s ORDER BY ts DESC",
                        (_ahora() - timedelta(hours=horas),)).fetchall()
    return {"hidratos": [_hidratos_json(f) for f in filas]}


# ------------------------------------------------------------------ Tresiba
@router.get("/tresiba")
def ver_tresiba(con=SESION):
    ahora = _ahora()
    prox = T.programadas_bd(con, ahora - timedelta(days=1), ahora + timedelta(days=2))
    return {"vigente": T.vigente(con, ahora), "historial": T.historial(con),
            "programadas": [{"ts": p["ts"].isoformat(), "ts_fin": p["ts_fin"].isoformat(),
                             "ts_estimada": p["ts_estimada"].isoformat(), "unidades": p["unidades"]}
                            for p in prox]}


@router.post("/tresiba")
def cambiar_tresiba(t: NuevaTresiba, con=SESION):
    desde = _con_zona(t.vigente_desde) if t.vigente_desde else _ahora()
    try:
        return {"vigente": T.guardar(con, t.unidades, t.hora_local, desde, t.notas,
                                       hora_fin_local=t.hora_fin_local)}
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Ya hay un cambio con esa misma fecha y hora")


# ------------------------------------------------------------------ cálculos
@router.get("/insulina-activa")
def insulina_activa(con=SESION):
    return insulina_activa_bd(con, _ahora())


@router.get("/metricas")
def metricas(dias: int = Query(14, ge=1, le=90), con=SESION):
    hasta = _ahora()
    return M.metricas(con, hasta - timedelta(days=dias), hasta)


def resumen_panel(con, ahora: datetime, aviso_pluma_horas: float = AP.UMBRAL_HORAS,
                  aviso_pluma_margen_min: int = AP.MARGEN_MIN) -> dict:
    """Lo que añade la fase 2 al panel."""
    pend = con.execute(   # una pareja cuenta como un solo caso
        """SELECT count(DISTINCT LEAST(id, COALESCE(dosis_par_id, id))) AS n FROM dosis WHERE requiere_revision"""
    ).fetchone()["n"]
    ultimas = con.execute(SQL_DOSIS + " WHERE d.ts >= %s ORDER BY d.ts DESC LIMIT 8",
                          (ahora - timedelta(hours=24),)).fetchall()
    return {
        "insulina_activa": insulina_activa_bd(con, ahora),
        "metricas_14d": M.metricas(con, ahora - timedelta(days=14), ahora),
        "por_revisar": pend,
        "aviso_pluma": AP.evaluar_bd(con, ahora, aviso_pluma_horas, aviso_pluma_margen_min),
        "tresiba": T.vigente(con, ahora),
        "ultimas_dosis": [_dosis_json(f) for f in ultimas],
        "curva": {"pico_min": FIASP.pico_min, "duracion_min": FIASP.duracion_min},
    }
