"""
Повний тест пайплайну: ZenRows знімок → фіксована обрізка → результат.
БЕЗ Gemini — використовує фіксовані пропорції з підтвердженого еталону.

Запуск:
    cd /opt/ocinka
    venv/bin/python scripts/test_full_pipeline.py

Результати:
    /opt/ocinka/pipeline_N_raw.jpg      — сирий знімок
    /opt/ocinka/pipeline_N_cropped.jpg  — після обрізки
"""
import asyncio
import sys
import json
import io as _io
from pathlib import Path
sys.path.insert(0, ".")


# ========================= ПАРАМЕТРИ ZENROWS ==========================
WAIT_MS = 3800
WINDOW_WIDTH = 900
WINDOW_HEIGHT = 1600
USE_FULLPAGE = False
ZOOM_FRACTION = 0.5
DEVICE = "desktop"

HIDE_CSS = (
    "var css="
    f"'body {{ zoom: {ZOOM_FRACTION} !important; }} "
    "[data-testid=\"cookies-bar\"],[data-testid*=\"cookie\"],"
    "[class*=\"cookie\"],[class*=\"Cookie\"],[id*=\"cookie\"],"
    "[class*=\"consent\"],[class*=\"Consent\"],[id*=\"consent\"],"
    "[class*=\"gdpr\"],[class*=\"Gdpr\"]"
    "{display:none !important;visibility:hidden !important;height:0 !important;}';"
    "var s=document.createElement('style');"
    "s.innerHTML=css;document.head.appendChild(s);"
)

# ================== ФІКСОВАНА ОБРІЗКА (без Gemini) ====================
# Пропорції виміряні з підтвердженого еталону "до/після":
# - 30% зліва: прибирає сіре тло OLX і лівий відступ
# - 70% справа: прибирає рекламну колонку і сіре тло справа
# - 7% зверху: прибирає шапку OLX (навігацію, пошук, банер реклами)
# - 93% знизу: прибирає "Зв'язатися з продавцем" і футер
CROP_LEFT_PCT = 30
CROP_TOP_PCT = 7
CROP_RIGHT_PCT = 70
CROP_BOTTOM_PCT = 93

# 3 різні оголошення
DEFAULT_URLS = [
    "https://www.olx.ua/d/uk/obyavlenie/prodam-2kmn-kvartiru-ID10cmh4.html",
    "https://www.olx.ua/d/uk/obyavlenie/prodaetsya-2-h-komnatnaya-kvartira-na-artema-IDYxSbX.html",
    "https://www.olx.ua/d/uk/obyavlenie/2-h-kmnatna-kvartira-IDW5HTL.html",
]


async def take_screenshot(client, url, index, apikey):
    """Крок 1: знімок через ZenRows."""
    instructions = [
        {"wait": 900},
        {"evaluate": HIDE_CSS},
        {"wait": 1500},
    ]
    params = {
        "apikey": apikey,
        "url": url,
        "screenshot": "true",
        "wait": WAIT_MS,
        "window_width": WINDOW_WIDTH,
        "window_height": WINDOW_HEIGHT,
        "js_render": "true",
        "device": DEVICE,
        "js_instructions": json.dumps(instructions),
    }
    if USE_FULLPAGE:
        params["screenshot_fullpage"] = "true"

    print(f"\n[{index}] {url}")
    print(f"  Крок 1: ZenRows знімок…")
    try:
        response = await client.get("https://api.zenrows.com/v1/", params=params)
    except Exception as e:
        print(f"    ЗАПИТ НЕ ВДАВСЯ: {type(e).__name__}: {e}")
        return None
    body = response.content
    is_image = (
        "image" in response.headers.get("content-type", "")
        or body.startswith(b"\x89PNG") or body.startswith(b"\xff\xd8")
    )
    if response.status_code != 200 or not is_image:
        print(f"    HTTP {response.status_code}. Тіло: {response.text[:200]}")
        return None

    raw_path = Path(f"/opt/ocinka/pipeline_{index}_raw.jpg")
    raw_path.write_bytes(body)
    from PIL import Image
    with Image.open(_io.BytesIO(body)) as im:
        size = im.size
    print(f"    OK. Знімок {size} -> {raw_path}")
    return raw_path


def crop_fixed(index, raw_path):
    """Крок 2: фіксована обрізка за пропорціями з еталону."""
    print(f"  Крок 2: Фіксована обрізка…")
    from PIL import Image
    with Image.open(raw_path) as im:
        w, h = im.size
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
        left = int(w * CROP_LEFT_PCT / 100)
        top = int(h * CROP_TOP_PCT / 100)
        right = int(w * CROP_RIGHT_PCT / 100)
        bottom = int(h * CROP_BOTTOM_PCT / 100)
        print(f"    Обрізка: left={left} top={top} right={right} bottom={bottom}")
        cropped = im.crop((left, top, right, bottom))
    out_path = Path(f"/opt/ocinka/pipeline_{index}_cropped.jpg")
    cropped.save(out_path, format="JPEG", quality=90)
    print(f"    Результат: {cropped.size} -> {out_path}")
    return out_path


async def process(client, url, index, apikey):
    raw_path = await take_screenshot(client, url, index, apikey)
    if raw_path:
        crop_fixed(index, raw_path)


async def main():
    from app.core.config import settings
    import httpx

    urls = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_URLS
    print(f"Пайплайн: {len(urls)} оголошень")
    print(f"ZenRows: вікно {WINDOW_WIDTH}x{WINDOW_HEIGHT}, zoom={ZOOM_FRACTION}")
    print(f"Обрізка: left={CROP_LEFT_PCT}% top={CROP_TOP_PCT}% "
          f"right={CROP_RIGHT_PCT}% bottom={CROP_BOTTOM_PCT}%")

    async with httpx.AsyncClient(timeout=90) as client:
        for i, url in enumerate(urls, start=1):
            await process(client, url, i, settings.zenrows_api_key)

    print("\nГотово. Порівняйте pipeline_N_raw.jpg vs pipeline_N_cropped.jpg через WinSCP.")


if __name__ == "__main__":
    asyncio.run(main())
