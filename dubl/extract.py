"""Крок 1 пайплайна: Gemini читає скриншот(и) оголошення і повертає параметри.

Самодостатній модуль (не імпортує ocinka). Паттерн виклику Gemini такий самий,
як у робочому ocr_service.py: legacy-клієнт google-generativeai, синхронний
generate_content у окремому потоці, суворий парсинг JSON без прози.
"""
from __future__ import annotations

import asyncio
import json
from io import BytesIO

import google.generativeai as genai

from dubl.config import settings


LISTING_PROMPT = """Ти аналізуєш 1–3 скриншоти ОДНОГО оголошення про нерухомість в Україні (OLX, DIM.RIA, сайт агентства тощо).
Витягни ТІЛЬКИ те, що видно на зображеннях. Нічого не вигадуй: якщо значення немає або нечитабельне — став null.

Поля (поверни рівно цей JSON, без markdown і без пояснень):
- object_type: один з "apartment", "house", "land", "commercial" або null (apartment = квартира, house = будинок, land = ділянка, commercial = комерція).
- city: населений пункт без "м." (місто/смт/село), або null.
- region: область, або null.
- district: район міста чи області, або null.
- street: вулиця, або null.
- building: номер будинку, або null.
- rooms: кількість кімнат, ціле число, або null.
- area_sqm: загальна площа в м², число (54.9, не "54,9 кв.м"), або null.
- floor: поверх, ціле, або null.
- total_floors: поверховість будинку, ціле, або null.
- price_value: ціна числом без пробілів і валюти, або null.
- price_currency: "USD", "UAH" або null.
- seller_type: "owner" якщо видно, що це приватна особа/власник ("від власника", "приватна особа"); "agency" якщо це агентство/бізнес (назва агентства, логотип, "агенція"); інакше null.
- agency_name: назва агентства, якщо видно в тексті, лого або водяному знаку на фото; інакше null.
- phone: номер телефону, якщо видно; інакше null.
- description_excerpt: короткий фрагмент опису (до 200 символів), або null.
- photos_visible: скільки РІЗНИХ фото самого об'єкта (кімнати, фасад, двір) видно на скриншотах, ціле число (0 якщо фото об'єкта немає).
- confidence: 0..1 — наскільки впевнено прочитано основні параметри.

Поверни ТІЛЬКИ валідний JSON із цими полями."""


def _parse_model_json(text: str) -> dict:
    """Дістати один JSON-об'єкт з відповіді моделі, не приймаючи прозу."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0].strip()
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]
    return json.loads(text)


def _prep_image(raw: bytes, mime: str) -> tuple[bytes, str]:
    """Стиснути великий скрин перед відправкою в Gemini (довга сторона 2200px)."""
    if mime not in {"image/jpeg", "image/png"} or len(raw) <= 1_500_000:
        return raw, mime
    try:
        from PIL import Image, ImageOps

        with Image.open(BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            out = BytesIO()
            image.save(out, format="JPEG", quality=88, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception as error:
        print(f"dubl extract: image compress skipped: {error}", flush=True)
        return raw, mime


async def extract_subject(images: list[tuple[bytes, str]]) -> dict:
    """Повертає параметри об'єкта зі скриншотів оголошення через Gemini.

    images — список (bytes, mime_type) по одному на кожен завантажений скрин.
    """
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY не налаштовано в .env.dubl")
    if not images:
        raise ValueError("Жодного зображення не передано")

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)

    content: list = [LISTING_PROMPT]
    for raw, mime in images:
        data, m = _prep_image(raw, mime)
        content.append({"mime_type": m, "data": data})

    response = await asyncio.wait_for(
        asyncio.to_thread(
            model.generate_content,
            content,
            request_options={"timeout": settings.gemini_timeout_seconds},
        ),
        timeout=settings.gemini_timeout_seconds + 10,
    )
    subject = _parse_model_json(response.text)
    subject["_provider"] = "gemini"
    return subject
