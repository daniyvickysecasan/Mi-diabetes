"""Aplicación FastAPI.

  uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api import auth, glucosa
from comun.ajustes import Ajustes
from comun.bd import conectar

PWA = Path(__file__).resolve().parent.parent / "pwa"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")


def crear_app(ajustes: Ajustes | None = None) -> FastAPI:
    app = FastAPI(title="Mi Diabetes", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ajustes = ajustes or Ajustes.desde_entorno()

    @app.middleware("http")
    async def cabeceras(request: Request, call_next):
        r = await call_next(request)
        r.headers["Content-Security-Policy"] = CSP
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["Referrer-Policy"] = "no-referrer"
        r.headers["Permissions-Policy"] = "camera=(self), geolocation=(), microphone=()"
        if request.url.path.startswith(("/api", "/auth")):
            r.headers["Cache-Control"] = "no-store"
        return r

    @app.get("/salud", include_in_schema=False)
    def salud():
        try:
            with conectar(app.state.ajustes.database_url) as con:
                con.execute("SELECT 1")
            return {"ok": True}
        except Exception:
            return JSONResponse({"ok": False, "error": "base de datos no disponible"}, status_code=503)

    app.include_router(auth.router)
    app.include_router(glucosa.router)
    app.mount("/", StaticFiles(directory=PWA, html=True), name="pwa")
    return app


app = crear_app()
