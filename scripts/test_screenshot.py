"""
Ізольований тест знімків ZenRows — без запуску повного пайплайну.
Прогоняє КІЛЬКА оголошень підряд і зберігає окремий файл для кожного.

Запуск з сервера:
    cd /opt/ocinka
    venv/bin/python scripts/test_screenshot.py

Можна також передати свій URL(и) аргументами:
    venv/bin/python scripts/test_screenshot.py "url1" "url2"

Результати: /opt/ocinka/test_shot_1.jpg, test_shot_2.jpg, test_shot_3.jpg
— скачайте через WinSCP і подивіться очима.
"""
import asyncio
import sys
import json
import io as _io
sys.path.insert(0, ".")


# =========================== ПАРАМЕТРИ ТЕСТУ ===========================
WAIT_MS = 3800
WINDOW_WIDTH = 900           # ширина вікна браузера
WINDOW_HEIGHT = 1600         # висота вікна браузера
USE_FULLPAGE = False         # False = знімок ВІКНА (не всієї сторінки)
SCROLL_Y = 0                 # поки без прокрутки — подивимось що вийде
DEVICE = "desktop"

HIDE_COOKIE_CSS = True       # CSS ховає cookie-банер (вже підтверджено що працює)

# Масштаб сторінки перед знімком. 50 = усе вдвічі менше (більше вмісту в кадрі).
# 100 = натуральний масштаб. 70-80 = гарний баланс читабельності і охоплення.
ZOOM_PERCENT = 50

# Обрізка після знімка — поки мінімальна, спочатку подивимось сирий знімок:
CROP_TOP_PX = 0              # 0 = нічого не різати зверху
CROP_SIDE_PX = 0             # 0 = нічого не різати з боків
KEEP_HEIGHT_PX = 0           # 0 = не різати низ

# 3 РІЗНІ оголошення для перевірки. Заміняйте на свої за потреби.
DEFAULT_URLS = [
    "https://www.olx.ua/d/uk/obyavlenie/prodaetsya-2-komntnaya-kvartira-tsentr-slavyansk-IDXJ6ja.html",
    "https://www.olx.ua/d/uk/obyavlenie/prodazha-2h-komnatnoy-kvartiry-rayon-artema-IDViIhA.html",
    "https://www.olx.ua/d/uk/obyavlenie/2-h-kmnatna-kvartira-IDW5HTL.html",
]
# =========================================================================


HIDE_CSS = (
    "var css="
    f"'body {{ zoom: {ZOOM_PERCENT/100.0} !important; }} "
    "[data-testid=\"cookies-bar\"],[data-testid*=\"cookie\"],"
    "[class*=\"cookie\"],[class*=\"Cookie\"],[id*=\"cookie\"],"
    "[class*=\"consent\"],[class*=\"Consent\"],[id*=\"consent\"],"
    "[class*=\"gdpr\"],[class*=\"Gdpr\"]"
    "{display:none !important;visibility:hidden !important;height:0 !important;}';"
    "var s=document.createElement('style');"
    "s.innerHTML=css;document.head.appendChild(s);"
)


def build_params(url):
    instructions = [{"wait": 900}]
    if HIDE_COOKIE_CSS:
        instructions.append({"evaluate": HIDE_CSS})
        # HIDE_CSS тепер містить і zoom, і приховування cookie одним CSS —
        # окрема інструкція для zoom більше не потрібна.
        instructions.append({"wait": 1500})
    if SCROLL_Y:
        instructions.append({"scroll_y": SCROLL_Y})
        instructions.append({"wait": 500})
    instructions.append({"wait": 800})
    params = {
        "apikey": None,  # заповнюється нижче
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
    return params


def crop_and_save(body, out_path):
    from PIL import Image
    im = Image.open(_io.BytesIO(body))
    before = im.size
    im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
    w, h = im.size
    left = CROP_SIDE_PX if w > 400 else 0
    right = max(left + 1, w - (CROP_SIDE_PX if w > 400 else 0))
    top = min(CROP_TOP_PX, max(0, h - 1))
    if KEEP_HEIGHT_PX and KEEP_HEIGHT_PX > 0:
        bottom = min(h, top + KEEP_HEIGHT_PX)
    else:
        bottom = h
    bottom = max(top + 1, bottom)
    cropped = im.crop((left, top, right, bottom))
    cropped.save(out_path, format="JPEG", quality=90)
    return before, cropped.size


async def shoot(client, url, index, apikey):
    params = build_params(url)
    params["apikey"] = apikey
    print(f"\n[{index}] {url}")
    try:
        response = await client.get("https://api.zenrows.com/v1/", params=params)
    except Exception as e:
        print(f"  ЗАПИТ НЕ ВДАВСЯ: {type(e).__name__}: {e}")
        return
    print("  HTTP статус:", response.status_code)
    body = response.content
    is_image = (
        "image" in response.headers.get("content-type", "")
        or body.startswith(b"\x89PNG") or body.startswith(b"\xff\xd8")
    )
    if response.status_code != 200 or not is_image:
        print("  ПОМИЛКА. Тіло (перші 300):", response.text[:300])
        return
    out_path = f"/opt/ocinka/test_shot_{index}.jpg"
    try:
        before, after = crop_and_save(body, out_path)
        print(f"  ОК. Розмір до обрізки {before} -> після {after}")
        print(f"  Збережено: {out_path}")
    except Exception as e:
        # якщо обрізка впала — збережемо хоч оригінал
        with open(out_path, "wb") as f:
            f.write(body)
        print(f"  Обрізка не вдалася ({e}); збережено оригінал: {out_path}")


async def main():
    from app.core.config import settings
    import httpx

    urls = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_URLS
    print(f"Тестуємо {len(urls)} оголошень.")
    print(f"Параметри: вікно {WINDOW_WIDTH}x{WINDOW_HEIGHT}, fullpage={USE_FULLPAGE}, "
          f"zoom={ZOOM_PERCENT}%, обрізка top={CROP_TOP_PX} side={CROP_SIDE_PX} "
          f"keep_height={KEEP_HEIGHT_PX}")

    async with httpx.AsyncClient(timeout=90) as client:
        for i, url in enumerate(urls, start=1):
            await shoot(client, url, i, settings.zenrows_api_key)

    print("\nГотово. Перевірте test_shot_1.jpg, test_shot_2.jpg, test_shot_3.jpg")


if __name__ == "__main__":
    asyncio.run(main())
