"""FastAPI application: config UI (+ scheduler is wired in Phase 5)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import scheduler
from db.session import init_db
from web.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="Twitter Summary Agent", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
