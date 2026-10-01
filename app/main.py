"""ocinka.pro — головний файл FastAPI"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.core.config import settings
from app.core.database import init_db
from app.api.routes import auth_router, reports_router, admin_router, partners_router

BUILD_ID = "2026-07-19-full-pdf-readable-screens-1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Створюємо таблиці при старті"""
    await init_db()
    yield


app = FastAPI(
    title="ocinka.pro API",
    description="Помічник для оцінювача нерухомості",
    version=BUILD_ID,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://ocinka.pro", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Роутери
app.include_router(auth_router)
app.include_router(reports_router)
app.include_router(admin_router)
app.include_router(partners_router)


@app.get("/api/health")
async def health():
    """Для UptimeRobot моніторингу"""
    return {"status": "ok", "service": "ocinka.pro", "build": BUILD_ID}
