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


LISTING_PROMPT = """Ти аналізуєш кілька скриншотів (1–10) ОДНОГО оголошення про нерухомість в Україні (OLX, DIM.RIA, сайт агентства тощо). На одних скриншотах — дані об'єкта, на інших — його фото.
Витягни ТІЛЬКИ те, що видно на зображеннях. Нічого не вигадуй: якщо значення немає або нечитабельне — став null.

Поля (поверни рівно цей JSON, без markdown і без пояснень):
- object_type: один з "apartment", "house", "land", "commercial" або null (apartment = квартира, house = будинок, land = ділянка, commercial = комерція).
- deal_type: "sale" (продаж) або "rent" (оренда) — визнач АВТОМАТИЧНО з тексту скриншота: слова "оренда"/"орендувати"/"здам"/"зняти"/"винайм"/ціна за місяць ("грн/міс", "$/міс") → rent; "продаж"/"продам"/"продається"/"продаю" → sale; null якщо незрозуміло.
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


TEXT_PROMPT = """Ти читаєш ПОВНИЙ ТЕКСТ одного оголошення про нерухомість в Україні (скопійований рядок/опис з OLX, DIM.RIA, rieltor.ua, сайту агентства тощо). Текст може містити посилання на це оголошення.
Витягни ТІЛЬКИ те, що реально є в тексті. Нічого не вигадуй: якщо значення немає — став null.

Поля (поверни рівно цей JSON, без markdown і без пояснень):
- object_type: один з "apartment", "house", "land", "commercial" або null.
- deal_type: "sale" (продаж) або "rent" (оренда) — визнач АВТОМАТИЧНО: "оренда"/"здам"/"зняти"/ціна за місяць → rent; "продаж"/"продам"/"продається" → sale; null якщо незрозуміло.
- city: населений пункт без "м.", або null.
- region: область, або null.
- district: район міста чи області, або null.
- street: вулиця, або null.
- building: номер будинку, або null.
- rooms: кількість кімнат, ціле, або null.
- area_sqm: загальна площа в м², число (54.9, не "54,9 кв.м"), або null.
- floor: поверх, ціле, або null.
- total_floors: поверховість будинку, ціле, або null.
- price_value: ціна числом без пробілів і валюти, або null.
- price_currency: "USD", "UAH" або null.
- seller_type: "owner" якщо приватна особа/власник; "agency" якщо агентство; інакше null.
- agency_name: назва агентства, якщо згадана; інакше null.
- phone: номер телефону, якщо є; інакше null.
- description_excerpt: короткий фрагмент опису (до 200 символів), або null.
- source_url: ПРЯМЕ посилання (http/https) на САМЕ це оголошення, якщо воно є в тексті; інакше null.
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
    # Не додаємо жодних позначок провайдера/ІІ у відповідь — користувач їх не бачить.
    subject.pop("_provider", None)
    return subject


async def extract_subject_from_text(text: str) -> dict:
    """Повертає параметри об'єкта з ПОВНОГО ТЕКСТУ оголошення через Gemini.

    Текст може містити посилання на оголошення — тоді повертаємо його у source_url,
    щоб потім підтягнути фото першоджерела для порівняння по фото.
    """
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY не налаштовано в .env.dubl")
    text = (text or "").strip()
    if not text:
        raise ValueError("Порожній текст оголошення")

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)

    response = await asyncio.wait_for(
        asyncio.to_thread(
            model.generate_content,
            [TEXT_PROMPT, "\n\nТЕКСТ ОГОЛОШЕННЯ:\n" + text[:8000]],
            request_options={"timeout": settings.gemini_timeout_seconds},
        ),
        timeout=settings.gemini_timeout_seconds + 10,
    )
    subject = _parse_model_json(response.text)
    subject.pop("_provider", None)
    # Підстрахуємося: якщо модель не дала source_url, знайдемо посилання в тексті самі.
    if not subject.get("source_url"):
        import re as _re
        m = _re.search(r'https?://[^\s"\'<>)\]]+', text)
        if m:
            subject["source_url"] = m.group(0).rstrip(".,;")
    return subject
