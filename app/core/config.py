"""Конфігурація застосунку — усі змінні з .env"""
from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://ocinka:password@localhost:5432/ocinka"
    
    # Redis
    redis_url: str = "redis://localhost:6379/0"
    
    # JWT
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 1440  # 24 hours
    algorithm: str = "HS256"
    
    # AI APIs
    gemini_api_key: str = ""
    # Configurable because Google retires model identifiers over time.
    gemini_model: str = "gemini-3.5-flash"
    anthropic_api_key: str = ""
    # Gemini extracts each document. Claude Sonnet verifies consistency across
    # the full document set; it never silently overrides the appraiser.
    claude_ocr_model: str = "claude-sonnet-4-6"
    claude_ocr_verify_enabled: bool = True
    # Upper bounds for remote AI calls.  A temporarily stalled provider must
    # not keep a report request running indefinitely.
    # OCR is an interactive step.  A stalled AI call must not keep the user
    # looking at a spinner for several minutes.
    gemini_ocr_timeout_seconds: int = 25
    claude_verify_timeout_seconds: int = 15
    ocr_concurrency: int = 6
    ocr_total_timeout_seconds: int = 50
    deepseek_api_key: str = ""
    
    # Data APIs
    dimria_api_key: str = ""
    # DIM.RIA is intentionally paused until its production API quota is enabled.
    # Set DIMRIA_ENABLED=true in .env only when it is ready for use.
    dimria_enabled: bool = False
    zenrows_api_key: str = ""
    scraper_api_key: str = ""
    # ScraperAPI is paused per product decision (2026-08): only ZenRows is
    # used for OLX/DIM.RIA scraping right now. Set SCRAPERAPI_ENABLED=true in
    # .env to turn it back on later; no code change needed either way.
    scraperapi_enabled: bool = False
    
    # Email
    brevo_api_key: str = ""
    
    # Payments
    lemonsqueezy_api_key: str = ""
    lemonsqueezy_webhook_secret: str = ""
    
    # Storage
    upload_dir: str = "/var/lib/ocinka/uploads"
    profile_documents_dir: str = "/var/lib/ocinka/profile-documents"
    object_photos_dir: str = "/var/lib/ocinka/object-photos"
    generated_dir: str = "/var/lib/ocinka/generated"
    screenshots_dir: str = "/var/lib/ocinka/screenshots"
    logs_dir: str = "/var/lib/ocinka/logs"
    screenshot_retention_hours: int = 24
    report_retention_days: int = 180

    # A browser is expensive.  This keeps simultaneous report generations
    # from starting dozens of Chromium processes on one VPS.
    browser_screenshot_concurrency: int = 2
    
    # Admin
    admin_username: str = "admin"
    admin_password: str = "change-me"
    
    # Sentry
    sentry_dsn: str = ""
    
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
