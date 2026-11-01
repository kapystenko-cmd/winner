"""dubl.top API — FastAPI, ізольований сервіс на власному порту.

M1: POST /api/analyze приймає до 10 скриншотів і повертає параметри об'єкта
(Gemini). Наступні етапи (кандидати з сайтів агентств, порівняння фото,
зріз цін DIM.RIA/OLX) додаються інкрементально без зміни цього контракту.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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


async def _vip_pipeline(subject: dict, reference_images: list[tuple[bytes, str]] | None) -> dict:
    """Спільне ядро VIP-пошуку: портали за даними → (за наявності еталонних фото)
    підтвердження по фото. reference_images — фото об'єкта (скриншоти або підтягнуті
    з посилання першоджерела). Якщо фото немає — показуємо кандидатів тільки за даними.
    """
    from dubl.portals import (search_dimria, search_olx, search_extra_portals,
                              apply_filters, usd_rate, confirm_by_photo, match_verdict)
    candidates: list = []
    photo_checked = False
    try:
        parts = await asyncio.gather(
            search_dimria(subject), search_olx(subject), search_extra_portals(subject),
            return_exceptions=True,
        )
        raw: list = []
        for name, part in zip(("dimria", "olx", "extra"), parts):
            if isinstance(part, Exception):
                print(f"dubl vip source {name} failed: {type(part).__name__}: {part}", flush=True)
            elif isinstance(part, list):
                raw += part
        candidates = apply_filters(subject, raw)

        # дедуп по url
        seen, uniq = set(), []
        for c in candidates:
            u = c.get("url") or ""
            if u and u in seen:
                continue
            seen.add(u)
            uniq.append(c)
        candidates = uniq

        # Якщо об'єкт у доларах — показуємо ціни кандидатів теж у $.
        if str(subject.get("price_currency") or "").upper() == "USD":
            rate = await usd_rate()
            for c in candidates:
                if c.get("price_uah"):
                    try:
                        c["price_usd"] = round(float(c["price_uah"]) / rate)
                    except (TypeError, ValueError):
                        pass

        # M3: підтвердження по фото — лише коли є еталонні фото об'єкта.
        if candidates and reference_images:
            photo_checked = True
            semaphore = asyncio.Semaphore(4)

            async def _confirm(c):
                async with semaphore:
                    try:
                        c["photo"] = await confirm_by_photo(reference_images, c)
                    except Exception as photo_error:
                        print(f"dubl vip confirm failed: {photo_error}", flush=True)
                        c["photo"] = {"match": "error", "confidence": 0, "matched": 0}

            await asyncio.gather(*[_confirm(c) for c in candidates[:10]])
            for c in candidates:
                c["verdict"] = match_verdict(subject, c)
            candidates = [c for c in candidates if c.get("verdict")]
            candidates.sort(
                key=lambda c: (c.get("verdict") == "original", (c.get("photo") or {}).get("confidence", 0)),
                reverse=True,
            )
    except Exception as error:
        import traceback
        print("dubl vip search error:\n" + traceback.format_exc(), flush=True)

    if photo_checked:
        note = ("«оригінал ✓» — той самий лістинг (дані збіглися точно); "
                "«той самий ✓» — підтверджено по фото. Решту відсіяно.")
    else:
        note = ("Фото об'єкта не було — показано збіги ЗА ДАНИМИ (місто, кімнати, "
                "поверх, площа ±10%). Для підтвердження по фото додай посилання на "
                "оголошення в текст або завантаж скриншоти.")
    return {
        "subject": subject,
        "count": len(candidates),
        "candidates": candidates,
        "photo_checked": photo_checked,
        "note": note,
    }


@app.post("/api/vip/search")
async def vip_search(files: list[UploadFile] = File(...)) -> dict:
    """VIP: пошук дублів по порталах за скриншотами (дані + фото)."""
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

    return await _vip_pipeline(subject, images)


@app.post("/api/vip/search-text")
async def vip_search_text(text: str = Form(""), url: str = Form("")) -> dict:
    """VIP: пошук дублів за ПОСИЛАННЯМ і/або ТЕКСТОМ оголошення.

    • Є посилання → підтягуємо з нього текст (якщо свого мало) і ВСІ фото як
      еталон, звіряємо дублі по фото.
    • Тільки текст → читаємо параметри, шукаємо за даними (+ фото, якщо в тексті
      трапиться посилання).
    """
    from dubl.extract import extract_subject_from_text
    from dubl.portals import fetch_photos_as_images, fetch_listing_text

    text = (text or "").strip()
    url = (url or "").strip()
    if not text and not url:
        raise HTTPException(status_code=400, detail="Встав посилання або текст оголошення")
    if url and not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url

    # Якщо дали посилання, а тексту мало — дочитуємо текст зі сторінки оголошення.
    if url and len(text) < 40:
        try:
            page_text = await fetch_listing_text(url)
        except Exception as error:
            print(f"dubl vip link text error: {error}", flush=True)
            page_text = ""
        text = (text + "\n" + page_text).strip()

    if len(text) < 15 and not url:
        raise HTTPException(status_code=400, detail="Замало даних для пошуку")

    try:
        subject = await extract_subject_from_text(text or url)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error))
    except Exception as error:
        print(f"dubl vip text extract error: {error}", flush=True)
        raise HTTPException(status_code=502, detail="Не вдалося прочитати оголошення")

    # Пряме посилання з поля має пріоритет над знайденим у тексті.
    src = url or subject.get("source_url")
    subject["source_url"] = src or None

    reference_images: list[tuple[bytes, str]] = []
    if src:
        try:
            reference_images = await fetch_photos_as_images(src, limit=8)
        except Exception as error:
            print(f"dubl vip text ref photos error: {error}", flush=True)

    result = await _vip_pipeline(subject, reference_images or None)
    result["source_url"] = src or None
    if src and not reference_images:
        result["note"] = ("Не вдалося підтягнути фото з посилання — показано збіги "
                          "ЗА ДАНИМИ. " + result["note"])
    return result
