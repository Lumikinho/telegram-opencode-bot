"""meu-bot worker: FastAPI que expõe turnos, bateria, funnel e render via HTTP."""
from fastapi import FastAPI

from . import config
from .routers import battery, funnel, render, turns

app = FastAPI(title="meu-bot worker", version="0.1.0")
app.include_router(turns.router)
app.include_router(battery.router)
app.include_router(funnel.router)
app.include_router(render.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def root() -> dict:
    return {"ok": True, "service": "meu-bot worker", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT,
                log_level=config.LOG_LEVEL)
