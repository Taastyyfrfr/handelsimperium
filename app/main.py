from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from app.database import init_pool, close_pool, get_db_connection
from app.csrf import CSRFMiddleware
from app.routes.auth_routes import router as auth_router
from app.routes.game_routes import router as game_router
from app.routes.resources import router as resources_router
from app.routes.buildings import router as buildings_router
from app.routes.market import router as market_router
from app.routes.trades import router as trades_router
from app.routes.ranking import router as ranking_router
from app.routes.notifications import router as notifications_router
from app.routes.contracts import router as contracts_router
from app.routes.guilds import router as guilds_router
from app.routes.tutorial import router as tutorial_router
from app.routes.handbook import router as handbook_router
from app.routes.admin import router as admin_router
from app.routes.caravans import router as caravans_router
from app.config import APP_VERSION
import os
import sys

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize connection pool, rate limits table & active auctions
    init_pool()
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS rate_limits (
                        id BIGSERIAL PRIMARY KEY,
                        client_key VARCHAR(128) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    CREATE INDEX IF NOT EXISTS idx_rate_limits_key_time ON rate_limits(client_key, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_rate_limits_key_created ON rate_limits(client_key, created_at);
                    ALTER TABLE kontor_auctions ADD COLUMN IF NOT EXISTS end_time TIMESTAMPTZ;
                    UPDATE kontor_auctions SET end_time = epoch_end_at WHERE end_time IS NULL;
                """)
                from app.engine.auctions import ensure_active_auctions
                ensure_active_auctions(cur)
                conn.commit()
    except Exception:
        pass
    yield
    # Shutdown: close connection pool
    close_pool()

app = FastAPI(
    title="Handelsimperium",
    description="Browser-based idle trading game with atomic order book",
    version=APP_VERSION,
    lifespan=lifespan,
)

# CSRF Protection Middleware
app.add_middleware(CSRFMiddleware)

# Static files directory (Manifest, Service Worker, Icons, CSS/JS)
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Include Routers
app.include_router(auth_router)
app.include_router(game_router)
app.include_router(resources_router)
app.include_router(buildings_router)
app.include_router(market_router)
app.include_router(trades_router)
app.include_router(ranking_router)
app.include_router(notifications_router)
app.include_router(contracts_router)
app.include_router(guilds_router)
app.include_router(tutorial_router)
app.include_router(handbook_router)
app.include_router(admin_router)
app.include_router(caravans_router)

# Inject global template variables into all router Jinja2 environments
for mod_name in [
    "app.routes.auth_routes",
    "app.routes.game_routes",
    "app.routes.resources",
    "app.routes.buildings",
    "app.routes.market",
    "app.routes.trades",
    "app.routes.ranking",
    "app.routes.notifications",
    "app.routes.contracts",
    "app.routes.guilds",
    "app.routes.tutorial",
    "app.routes.handbook",
    "app.routes.caravans",
]:
    mod = sys.modules.get(mod_name)
    if mod and hasattr(mod, "templates"):
        mod.templates.env.globals["APP_VERSION"] = APP_VERSION

@app.get("/health")
@app.head("/health")
@app.head("/")
def health_check():
    return {"status": "ok", "app": "Handelsimperium", "version": APP_VERSION}

