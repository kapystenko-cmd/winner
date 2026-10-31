"""dubl.top API — FastAPI, ізольований сервіс на власному порту.

M1: POST /api/analyze приймає до 10 скриншотів і повертає параметри об'єкта
(Gemini). Наступні етапи (кандидати з сайтів агентств, порівняння фото,
зріз цін DIM.RIA/OLX) додаються інкрементально без зміни цього контракту.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from dubl.config import settings
from dubl.extract import extract_subject

app = FastAPI(title="dubl.top API", version="0.1.0-m1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_MAX_FILES = 10
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "dubl.top", "stage": "M1"}


@app.post("/api/analyze")
async def analyze(files: list[UploadFile] = File(...)) -> dict:
    """Прийняти скриншоти оголошення і повернути розпізнані параметри об'єкта."""
    if not files:
        raise HTTPException(status_code=400, detail="Не передано жодного скриншота")

    images: list[tuple[bytes, str]] = []
    for upload in files[:_MAX_FILES]:
        mime = (upload.content_type or "image/jpeg").lower()
        if mime not in _ALLOWED_MIME:
            raise HTTPException(status_code=400, detail=f"Непідтримуваний формат: {mime}")
        raw = await upload.read()
        if not raw:
            continue
        if len(raw) > 15 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Файл завеликий (макс 15 МБ)")
        images.append((raw, mime))

    if not images:
        raise HTTPException(status_code=400, detail="Порожні файли")

    try:
        subject = await extract_subject(images)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except Exception as error:
        print(f"dubl /api/analyze error: {type(error).__name__}: {error}", flush=True)
        raise HTTPException(status_code=502, detail="Не вдалося проаналізувати скриншот")

    return {
        "stage": "extracted",
        "subject": subject,
        "next": "Пошук кандидатів і порівняння фото — наступні етапи (M2–M4).",
    }


@app.get("/api/agencies")
async def agencies(city: str) -> dict:
    """Агентства міста з каталогу + свіжий добір (Serper+OSM), що зростає з запитами."""
    from dubl import store
    from dubl.catalog import find_agencies_serper, find_agencies_osm
    await store.init()
    # Свіжий добір по місту (web-пошук + OSM), тільки з сайтом → у каталог.
    serper, osm = await asyncio.gather(
        find_agencies_serper(city), find_agencies_osm(city)
    )
    fresh = [{**a, "city": city} for a in (serper + osm) if a.get("website")]
    await store.upsert(fresh)
    # Повертаємо все, що є в каталозі по цьому місту (сид + накопичене).
    items = await store.by_city(city)
    return {"city": city, "count": len(items), "fresh_found": len(fresh), "agencies": items}


@app.get("/api/catalog/stats")
async def catalog_stats() -> dict:
    """Стан каталогу: скільки агентств усього, з сайтом, топ міст."""
    from dubl import store
    await store.init()
    return await store.stats()


async def _read_uploads(files: list[UploadFile]) -> list[tuple[bytes, str]]:
    images: list[tuple[bytes, str]] = []
    for upload in files[:_MAX_FILES]:
        mime = (upload.content_type or "image/jpeg").lower()
        if mime not in _ALLOWED_MIME:
            raise HTTPException(status_code=400, detail=f"Непідтримуваний формат: {mime}")
        raw = await upload.read()
        if not raw:
            continue
        if len(raw) > 15 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Файл завеликий (макс 15 МБ)")
        images.append((raw, mime))
    if not images:
        raise HTTPException(status_code=400, detail="Порожні файли")
    return images


@app.post("/api/vip/search")
async def vip_search(files: list[UploadFile] = File(...)) -> dict:
    """VIP: пошук дублів по порталах. Етап 1 — DOM.RIA за даними."""
    if not files:
        raise HTTPException(status_code=400, detail="Не передано жодного скриншота")
    images = await _read_uploads(files)
    try:
        subject = await extract_subject(images)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except Exception as error:
        print(f"dubl vip extract error: {error}", flush=True)
        raise HTTPException(status_code=502, detail="Не вдалося проаналізувати скриншот")

    from dubl.portals import search_dimria, search_olx, apply_filters
    dimria, olx = await asyncio.gather(search_dimria(subject), search_olx(subject))
    candidates = apply_filters(subject, dimria + olx)
    return {
        "subject": subject,
        "count": len(candidates),
        "candidates": candidates,
        "note": "Кандидати за даними (кімнати і поверх строго, площа ±10%). "
                "Підтвердження по фото і rieltor.ua — наступні етапи.",
    }
