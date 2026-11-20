"""ntfy simulado para tests: guarda lo que se publica y exige el token.

Solo para tests y demostraciones. Imita la publicación JSON de ntfy (POST / con {"topic", ...}).
"""
from fastapi import FastAPI, Header, HTTPException, Request


def crear_app(token: str = "", fallar: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.mensajes = []
    app.state.fallar = fallar

    @app.post("/")
    async def publicar(request: Request, authorization: str | None = Header(None)):
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "token")
        if app.state.fallar:
            raise HTTPException(503, "caído")
        cuerpo = await request.json()
        if not cuerpo.get("topic"):
            raise HTTPException(400, "topic")
        app.state.mensajes.append(cuerpo)
        return {"id": str(len(app.state.mensajes)), "event": "message", "topic": cuerpo["topic"]}

    return app
