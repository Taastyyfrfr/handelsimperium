from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from app.database import init_pool, close_pool
from app.routes.auth_routes import router as auth_router
from app.routes.game_routes import router as game_router
from app.routes.resources import router as resources_router
from app.routes.buildings import router as buildings_router
from app.routes.market import router as market_router
from app.routes.trades import router as trades_router
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize connection pool
    init_pool()
    yield
    # Shutdown: close connection pool
    close_pool()

app = FastAPI(
    title="Handelsimperium",
    description="Browser-based idle trading game with atomic order book",
    version="1.0.0",
    lifespan=lifespan,
)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Include Routers
app.include_router(auth_router)
app.include_router(game_router)
app.include_router(resources_router)
app.include_router(buildings_router)
app.include_router(market_router)
app.include_router(trades_router)

@app.get("/health")
@app.head("/health")
@app.head("/")
def health_check():
    return {"status": "ok", "app": "Handelsimperium"}
