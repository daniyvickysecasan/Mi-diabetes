"""Nightscout simulado (API v1 de solo lectura) para tests y para probar sin datos reales.

Implementa lo que usa el recolector:
  GET /api/v1/entries.json     find[date][$gte|$gt|$lt|$lte], count  (orden: más reciente primero)
  GET /api/v1/treatments.json  find[created_at][$gte|$gt|$lt|$lte], count
y además, solo en el simulador:
  POST /simulado/lectura       añade una lectura "ahora" (o con ?valor=)
  POST /api/v1/treatments      crea tratamientos (token de escritura; upsert por created_at+eventType,
                               como Nightscout)

Arranque:  NS_TOKEN=prueba-lectura python -m herramientas.nightscout_simulado
Con NS_EN_VIVO=1 añade una lectura cada 5 minutos, como LibreLinkUp.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request

from herramientas import sinteticos


def _filtrar(datos, campo, params, convertir):
    ops = {"$gte": lambda a, b: a >= b, "$gt": lambda a, b: a > b,
           "$lt": lambda a, b: a < b, "$lte": lambda a, b: a <= b}
    for op, f in ops.items():
        v = params.get(f"find[{campo}][{op}]")
        if v is not None:
            limite = convertir(v)
            datos = [d for d in datos if d.get(campo) is not None and f(convertir(d[campo]), limite)]
    return datos


def _iso(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def crear_app(entradas: list[dict], tratamientos: list[dict], token: str = "", en_vivo: bool = False,
              token_escritura: str = "") -> FastAPI:
    estado = {"entradas": list(entradas), "tratamientos": list(tratamientos), "peticiones": 0}

    async def _vivo():
        while True:
            await asyncio.sleep(300)
            estado["entradas"].append(sinteticos.nueva_lectura(sinteticos.ahora_redondeado()))

    @asynccontextmanager
    async def ciclo(_):
        tarea = asyncio.create_task(_vivo()) if en_vivo else None
        yield
        if tarea:
            tarea.cancel()

    app = FastAPI(title="Nightscout simulado", lifespan=ciclo)
    app.state.datos = estado

    def comprobar(request: Request):
        estado["peticiones"] += 1
        if token and request.query_params.get("token") not in (token, token_escritura or None):
            raise HTTPException(401, "Unauthorized")

    @app.get("/api/v1/entries.json")
    def entries(request: Request):
        comprobar(request)
        p = request.query_params
        datos = _filtrar(estado["entradas"], "date", p, float)
        datos.sort(key=lambda d: d["date"], reverse=True)
        return datos[: int(p.get("count", 10))]

    @app.get("/api/v1/treatments.json")
    def treatments(request: Request):
        comprobar(request)
        p = request.query_params
        datos = _filtrar(estado["tratamientos"], "created_at", p, _iso)
        datos.sort(key=lambda d: d["created_at"], reverse=True)
        return datos[: int(p.get("count", 100))]

    @app.post("/api/v1/treatments")
    async def crear_tratamientos(request: Request):
        estado["peticiones"] += 1
        if not token_escritura or request.query_params.get("token") != token_escritura:
            raise HTTPException(401, "Unauthorized")
        cuerpo = await request.json()
        nuevos = cuerpo if isinstance(cuerpo, list) else [cuerpo]
        hechos = []
        for t in nuevos:
            previo = next((x for x in estado["tratamientos"] if x.get("created_at") == t.get("created_at")
                           and x.get("eventType") == t.get("eventType")), None)
            if previo:
                previo.update(t)
                hechos.append(previo)
            else:
                t = dict(t, _id=f"w{len(estado['tratamientos']) + 1}")
                estado["tratamientos"].append(t)
                hechos.append(t)
        return hechos

    @app.post("/simulado/lectura")
    def lectura(valor: int | None = None):
        e = sinteticos.nueva_lectura(datetime.now(timezone.utc).replace(microsecond=0))
        if valor is not None:
            e["sgv"] = valor
        estado["entradas"].append(e)
        return e

    return app


def app_por_defecto() -> FastAPI:
    ahora = sinteticos.ahora_redondeado()
    dias = int(os.environ.get("NS_DIAS", "30"))
    entradas, tratamientos = sinteticos.generar(ahora - timedelta(days=dias), ahora)
    return crear_app(entradas, tratamientos, os.environ.get("NS_TOKEN", ""),
                     en_vivo=os.environ.get("NS_EN_VIVO", "1") == "1",
                     token_escritura=os.environ.get("NS_TOKEN_ESCRITURA", ""))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app_por_defecto(), host="0.0.0.0", port=int(os.environ.get("NS_PUERTO", "1337")))
