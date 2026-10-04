"""One FastAPI process, one MCP lifespan, SQLAlchemy storage and sqladmin."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from research_engine.admin.views import mount_admin
from research_engine.config import Settings, load_config
from research_engine.router.execute import Engine
from research_engine.server.auth import MCPAuthentication
from research_engine.server.tools import register_tools


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    settings = settings or load_config()
    settings.validate_bootstrap()
    engine = engine or Engine(settings)
    mcp = register_tools(engine)
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            await engine.stop()

    app = FastAPI(title="Research Engine", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.engine, app.state.mcp = engine, mcp

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    mount_admin(app, settings, engine.db, engine.secrets, engine=engine, oauth_manager=engine.mcp_manager)
    app.add_middleware(MCPAuthentication, db=engine.db, settings=settings)
    app.mount("/", mcp_app)
    return app
