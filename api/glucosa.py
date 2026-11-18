"""Endpoints de glucosa y del panel. Todos exigen sesión."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request

from api.deps import requiere_sesion
from api.dosis import resumen_panel
from comun import glucosa as G

router = APIRouter(prefix="/api", tags=["glucosa"])
SESION = Depends(requiere_sesion, scope="function")


def _ahora():
    return datetime.now(timezone.utc)


@router.get("/glucosa/actual")
def actual(con=SESION):
    return {"actual": G.actual(con)}


@router.get("/glucosa")
def serie(horas: int = Query(24, ge=1, le=24 * 14), con=SESION):
    hasta = _ahora()
    return {"lecturas": G.serie(con, hasta - timedelta(hours=horas), hasta)}


@router.get("/glucosa/tir")
def tir(request: Request, horas: int = Query(24, ge=1, le=24 * 90), con=SESION):
    aj = request.app.state.ajustes
    hasta = _ahora()
    return G.tir(con, hasta - timedelta(hours=horas), hasta, aj.rango_bajo, aj.rango_alto)


@router.get("/panel")
def panel(request: Request, con=SESION):
    """Todo lo que pinta el panel, en una sola llamada."""
    aj = request.app.state.ajustes
    hasta = _ahora()
    ultima = con.execute(
        """SELECT fin, error FROM ejecuciones_recoleccion WHERE fuente = 'nightscout'
           ORDER BY inicio DESC LIMIT 1""").fetchone()
    huecos = con.execute(
        "SELECT inicio, minutos FROM huecos WHERE fuente = 'nightscout' AND estado = 'abierto' ORDER BY inicio DESC LIMIT 1"
    ).fetchone()
    return {
        "actual": G.actual(con, ahora=hasta),
        "serie_24h": G.serie(con, hasta - timedelta(hours=24), hasta),
        "tir_24h": G.tir(con, hasta - timedelta(hours=24), hasta, aj.rango_bajo, aj.rango_alto),
        "tir_14d": G.tir(con, hasta - timedelta(days=14), hasta, aj.rango_bajo, aj.rango_alto),
        "rango_mgdl": [aj.rango_bajo, aj.rango_alto],
        "recoleccion": {
            "ultima": ultima["fin"].isoformat() if ultima and ultima["fin"] else None,
            "error": ultima["error"] if ultima else None,
            "hueco_abierto": {"desde": huecos["inicio"].isoformat(), "minutos": huecos["minutos"]} if huecos else None,
        },
        **resumen_panel(con, hasta),
    }
