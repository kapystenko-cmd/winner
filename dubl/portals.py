"""VIP: пошук дублів по великих порталах (особистий інструмент).

Етап 1 — DOM.RIA через офіційний API: кандидати за даними
(місто, тип, кількість кімнат — строго, площа ±10%).
Далі: OLX (ZenRows + фільтри + усі сторінки), rieltor.ua, підтвердження по фото.

Використовує власні ключі з .env.dubl (dimria_api_key, zenrows_api_key).
Логіка DOM.RIA портована зі стабільного коду ocinka (dimria_service).
"""
from __future__ import annotations

import asyncio
import re

import httpx

from dubl.config import settings

DIMRIA_BASE = "https://developers.ria.com/dom"
_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
}
# category/realty_type/characteristic — як у документації Developers.RIA.
_SPEC = {
    "apartment": {"category": 1, "realty_type": 2, "rooms_ch": 209},
    "house": {"category": 4, "rooms_ch": 209},
    "land": {"category": 5},
}


def _num(value) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9,.-]", "", str(value)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _key(value: str | None) -> str:
    """Звести назву міста до порівнюваного ключа (як у ocinka)."""
    value = str(value or "").casefold().strip()
    value = re.sub(r"^(м\.?|місто|г\.?|город)\s*", "", value)
    value = re.sub(r"\b(область|обл|район|р-н)\b\.?", "", value)
    value = value.replace("’", "'").replace("-", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .,;:").strip()


async def _resolve_city(city: str, client: httpx.AsyncClient) -> tuple[int, int] | None:
    wanted = _key(city)
    if not wanted:
        return None
    resp = await client.get(DIMRIA_BASE + "/states", params={"api_key": settings.dimria_api_key, "lang_id": 4})
    resp.raise_for_status()
    states = resp.json() or []
    # обласні центри — без запиту списку міст
    for state in states:
        aliases = (state.get("region_name"), state.get("center_declension"), state.get("name"))
        if wanted in {_key(a) for a in aliases if a}:
            sid = int(state["stateID"])
            return sid, sid
    # інші міста — через /cities/{stateId}
    semaphore = asyncio.Semaphore(5)

    async def find(state: dict) -> tuple[int, int] | None:
        async with semaphore:
            sid = int(state["stateID"])
            r = await client.get(DIMRIA_BASE + f"/cities/{sid}", params={"api_key": settings.dimria_api_key, "lang_id": 4})
            r.raise_for_status()
            for item in (r.json() or []):
                if wanted == _key(item.get("name")):
                    return sid, int(item["cityID"])
        return None

    tasks = [asyncio.create_task(find(s)) for s in states]
    try:
        for task in asyncio.as_completed(tasks):
            found = await task
            if found:
                return found
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
    return None


async def _detail(item_id, client: httpx.AsyncClient) -> dict | None:
    try:
        resp = await client.get(DIMRIA_BASE + f"/info/{item_id}", params={"api_key": settings.dimria_api_key, "lang_id": 4})
        resp.raise_for_status()
        data = resp.json() or {}
        price_arr = data.get("priceArr") or {}
        price_uah = _num(price_arr.get("3"))
        slug = str(data.get("beautiful_url") or item_id)
        if not slug.endswith(".html"):
            slug += ".html"
        street = data.get("street_name_uk") or data.get("street_name") or ""
        building = data.get("building_number_str") or ""
        return {
            "source": "dimria",
            "url": "https://dom.ria.com/uk/" + slug,
            "title": data.get("description_uk") or data.get("realty_type_name") or "",
            "price_uah": round(price_uah) if price_uah else None,
            "area_sqm": _num(data.get("total_square_meters")) or None,
            "rooms": data.get("rooms_count"),
            "floor": data.get("floor"),
            "address": (str(street) + " " + str(building)).strip(),
            "city": data.get("city_name_uk") or data.get("city_name") or "",
        }
    except Exception as error:
        print(f"dubl vip dimria detail {item_id}: {error}", flush=True)
        return None


async def search_dimria(subject: dict, limit: int = 40) -> list[dict]:
    """Кандидати з DOM.RIA за даними: місто, тип, кімнати строго, площа ±10%."""
    if (not settings.dimria_api_key or not settings.dimria_enabled
            or settings.dimria_api_key == "your-dimria-api-key"):
        return []
    city = subject.get("city")
    otype = str(subject.get("object_type") or "apartment")
    rooms = subject.get("rooms")
    area = subject.get("area_sqm")
    spec = _SPEC.get(otype)
    if not spec or not city:
        return []
    try:
        async with httpx.AsyncClient(timeout=30, headers=_HDRS) as client:
            resolved = await _resolve_city(city, client)
            if not resolved:
                print(f"dubl vip dimria: city not resolved: {city}", flush=True)
                return []
            state_id, city_id = resolved
            params: dict = {
                "api_key": settings.dimria_api_key,
                "category": spec["category"],
                "operation_type": 1,  # продаж
                "city_id": city_id,
                "state_id": state_id,
                "page": 0,
                "limit": min(max(1, limit), 60),
            }
            if spec.get("realty_type"):
                params["realty_type"] = spec["realty_type"]
            if rooms and spec.get("rooms_ch"):
                params[f"characteristic[{spec['rooms_ch']}][from]"] = int(rooms)
                params[f"characteristic[{spec['rooms_ch']}][to]"] = int(rooms)
            resp = await client.get(DIMRIA_BASE + "/search", params=params)
            resp.raise_for_status()
            ids = list((resp.json() or {}).get("items") or [])[:limit]
            out = []
            for item_id in ids:
                detail = await _detail(item_id, client)
                if detail:
                    out.append(detail)
                await asyncio.sleep(0.3)  # DOM.RIA 429-захист
    except Exception as error:
        print(f"dubl vip dimria error: {type(error).__name__}: {error}", flush=True)
        return []

    # площа ±10% (кімнати вже строго відфільтровані пошуком)
    if area:
        tol = settings.area_tolerance
        low, high = area * (1 - tol), area * (1 + tol)
        out = [x for x in out if not x.get("area_sqm") or low <= x["area_sqm"] <= high]
    return out


def _as_int(value) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def apply_filters(subject: dict, candidates: list[dict]) -> list[dict]:
    """Строгий відбір кандидатів: кімнати і поверх збігаються ТОЧНО; площа ±10%.
    (Фінальне підтвердження — по фото, окремий етап.)"""
    sr = _as_int(subject.get("rooms"))
    sf = _as_int(subject.get("floor"))
    try:
        area = float(subject.get("area_sqm")) if subject.get("area_sqm") not in (None, "") else None
    except (TypeError, ValueError):
        area = None
    tol = settings.area_tolerance
    out = []
    for c in candidates:
        cr = _as_int(c.get("rooms"))
        if sr and cr and cr != sr:
            continue
        cf = _as_int(c.get("floor"))
        if sf and cf and cf != sf:          # поверх строго, якщо відомий в обох
            continue
        if area and c.get("area_sqm"):
            try:
                a = float(c["area_sqm"])
                if not (area * (1 - tol) <= a <= area * (1 + tol)):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(c)
    return out


def _street_key(value) -> str:
    v = str(value or "").casefold()
    v = re.sub(r"\b(вул(иця)?|просп(ект)?|пров(улок)?|бульв(ар)?|наб(ережна)?|пл(оща)?|"
               r"ул(ица)?|пер(еулок)?|street|st|ave)\b\.?", "", v)
    v = re.sub(r"[^a-zа-яіїєґ0-9 ]+", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def _data_strength(subject: dict, c: dict) -> str:
    """Наскільки СИЛЬНО збігаються ДАНІ (без фото): 'exact' | 'strong' | 'weak'.
    exact — той самий лістинг; strong — дуже ймовірно той самий об'єкт."""
    sr, cr = _as_int(subject.get("rooms")), _as_int(c.get("rooms"))
    try:
        sa = float(subject.get("area_sqm")) if subject.get("area_sqm") not in (None, "") else None
    except (TypeError, ValueError):
        sa = None
    try:
        ca = float(c.get("area_sqm")) if c.get("area_sqm") not in (None, "") else None
    except (TypeError, ValueError):
        ca = None
    area_near = bool(sa and ca and abs(ca - sa) / sa <= 0.03)
    rooms_ok = bool(sr and cr and sr == cr)

    # ціна
    sp = subject.get("price_value")
    cp = c.get("price_usd") if str(subject.get("price_currency") or "").upper() == "USD" else c.get("price_uah")
    price_near = None
    try:
        if sp and cp:
            price_near = abs(float(cp) - float(sp)) / float(sp) <= 0.05
    except (TypeError, ValueError, ZeroDivisionError):
        price_near = None

    # вулиця
    s_str, c_str = _street_key(subject.get("street")), _street_key(c.get("street") or c.get("address"))
    street_ok = bool(s_str and c_str and (s_str in c_str or c_str in s_str))

    if rooms_ok and area_near and price_near is True:
        return "exact"
    if (rooms_ok and area_near and (street_ok or price_near is True)) or (street_ok and area_near):
        return "strong"
    return "weak"


def match_verdict(subject: dict, c: dict) -> str | None:
    """Вердикт по кандидату:
      • 'original' — підтверджено по фото ТА дані ідентичні (той самий лістинг);
      • 'dup'      — підтверджено по фото (той самий об'єкт);
      • 'maybe'    — фото звірити не вдалося, але ДАНІ сильно збігаються
                     (та сама вулиця або площа+ціна) → ймовірний дубль за даними;
      • None       — фото сказало «інший об'єкт», або дані збігаються слабо.
    """
    photo = (c.get("photo") or {}).get("match")
    strength = _data_strength(subject, c)
    if photo == "yes":
        return "original" if strength == "exact" else "dup"
    if photo == "no":
        return None  # Gemini явно сказав: інший об'єкт
    # Фото не вдалося перевірити (no_photos / error / no_key).
    # Показуємо лише СИЛЬНІ збіги за даними, щоб не засмічувати таблицю.
    if strength in ("exact", "strong"):
        return "maybe"
    return None


def _looks_like_listing(url: str) -> bool:
    """Відсікти сторінки пошуку/категорій (лишаємо тільки окремі оголошення)."""
    low = (url or "").lower()
    if any(b in low for b in ("q-", "/list/", "/search", "/filter", "/poshuk", "/poisk")):
        return False
    last = low.rstrip("/").rsplit("/", 1)[-1]
    return any(ch.isdigit() for ch in last) or "id" in last


# ---------------------------------------------------------------- OLX
def _ensure_ocinka_env() -> None:
    """Прокинути ключі dubl у os.environ, щоб app.core.config (ocinka) їх побачив.
    Робимо ДО першого імпорту app.core.config (він створює Settings() на імпорті).
    """
    import os
    for name, value in {
        "ZENROWS_API_KEY": settings.zenrows_api_key,
        "GEMINI_API_KEY": settings.gemini_api_key,
        "GEMINI_MODEL": settings.gemini_model,
    }.items():
        if value and not os.environ.get(name):
            os.environ[name] = value


async def usd_rate() -> float:
    """Курс USD→UAH (переиспользуємо get_usd_rate ocinka)."""
    _ensure_ocinka_env()
    try:
        from app.services.olx_service import get_usd_rate
        return float(await get_usd_rate())
    except Exception as error:
        print(f"dubl vip usd_rate error: {error}", flush=True)
        return 41.5


async def search_olx(subject: dict, limit: int = 60) -> list[dict]:
    """Кандидати з OLX через ZenRows — переиспользуємо перевірений olx_service ocinka."""
    if not settings.zenrows_api_key:
        return []
    city = subject.get("city")
    otype = str(subject.get("object_type") or "apartment")
    if not city:
        return []
    _ensure_ocinka_env()
    try:
        from app.services.olx_service import find_olx_analogs
    except Exception as error:
        print(f"dubl vip olx import error: {type(error).__name__}: {error}", flush=True)
        return []
    try:
        results = await find_olx_analogs(
            city, otype, subject.get("rooms"), subject.get("area_sqm"),
            report_id="", max_pages=3, max_candidates=limit, screenshots=False,
            district=subject.get("district"), price_min_uah=None, price_max_uah=None,
            region=subject.get("region"),
        )
    except Exception as error:
        print(f"dubl vip olx error: {type(error).__name__}: {error}", flush=True)
        return []

    out = []
    for r in (results or []):
        out.append({
            "source": "olx",
            "url": r.get("url") or r.get("link") or "",
            "title": r.get("title") or "",
            "price_uah": r.get("price_uah") or r.get("price"),
            "area_sqm": r.get("area_sqm") or r.get("area"),
            "rooms": r.get("rooms"),
            "floor": r.get("floor"),
            "address": r.get("address") or "",
        })
    area = subject.get("area_sqm")
    if area:
        tol = settings.area_tolerance
        low, high = area * (1 - tol), area * (1 + tol)
        out = [x for x in out if not x.get("area_sqm") or low <= float(x["area_sqm"]) <= high]
    return out


# ---------------------------------------------------------------- M3: фото
import re as _re

_OG_IMG_RE = _re.compile(r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)', _re.I)
_OLX_IMG_RE = _re.compile(r'https://[a-z0-9.\-]*ol(?:x)?cdn\.com/[^\s"\'\\<>]+?\.(?:jpe?g|png|webp)', _re.I)
_RIA_IMG_RE = _re.compile(r'https://cdn[0-9]*\.riastatic\.com/[^\s"\'\\<>]+?\.(?:jpe?g|png|webp)', _re.I)
# Універсальний пошук фото (будь-який CDN) + відсів логотипів/іконок.
_IMG_ANY_RE = _re.compile(r'https?://[^\s"\'<>()]+?\.(?:jpe?g|png|webp)(?:\?[^\s"\'<>()]*)?', _re.I)
_IMG_SKIP = ("logo", "icon", "sprite", "avatar", "placeholder", "favicon",
             "no-image", "noimage", "blank", "/svg", "emoji", "flag")

_PHOTO_PROMPT = (
    "Порівняй ДВА набори фото. ПЕРШИЙ — фото об'єкта зі скриншотів оголошення користувача. "
    "ДРУГИЙ — фото іншого оголошення (кандидата).\n"
    "УВАГА: більшість квартир РІЗНІ, навіть якщо схожий ремонт, планування, меблі чи тип квартири. "
    "Схожість — це НЕ той самий об'єкт.\n"
    "Відповідай same_object=true ТІЛЬКИ якщо це ФІЗИЧНО ТА САМА квартира: видно ті самі КОНКРЕТНІ фото "
    "(нехай обрізані, дзеркальні, з водяним знаком, іншим кадруванням) АБО однозначно ті самі приміщення "
    "з унікальними деталями, що збігаються (та сама плитка/підлога, той самий вид з вікна, ті самі меблі на тих самих місцях, "
    "ті самі дефекти чи особливості).\n"
    "Потрібно щонайменше 2 фото, що однозначно збігаються. Якщо є хоч сумнів — same_object=false.\n"
    "Поверни ТІЛЬКИ JSON: {\"same_object\": true/false, \"matched_photos\": <ціле>, \"confidence\": <0..1>}."
)


async def _fetch_html(url: str) -> str:
    if settings.zenrows_api_key:
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.get("https://api.zenrows.com/v1/", params={
                    "apikey": settings.zenrows_api_key, "url": url,
                    "js_render": "true", "wait": 3000,
                })
                r.raise_for_status()
                return r.text
        except Exception as error:
            print(f"dubl vip fetch_html zr error: {error}", flush=True)
    try:
        async with httpx.AsyncClient(timeout=25, headers=_HDRS, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except Exception as error:
        print(f"dubl vip fetch_html error: {error}", flush=True)
        return ""


async def fetch_listing_photos(url: str, limit: int = 5) -> list[str]:
    html = await _fetch_html(url)
    if not html:
        return []
    urls: list[str] = []
    m = _OG_IMG_RE.search(html)
    if m:
        urls.append(m.group(1))
    for u in _IMG_ANY_RE.findall(html):
        low = u.lower()
        if any(s in low for s in _IMG_SKIP):
            continue
        if u not in urls:
            urls.append(u)
    return urls[:limit]


async def _download_image(url: str, client: httpx.AsyncClient):
    try:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        ct = (r.headers.get("content-type") or "").split(";")[0].strip() or "image/jpeg"
        if "image" not in ct or len(r.content) < 1500:
            return None
        return r.content, ct
    except Exception:
        return None


_TAG_RE = _re.compile(r"<[^>]+>")
_WS_RE = _re.compile(r"\s+")
_SCRIPT_RE = _re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", _re.I | _re.S)


async def fetch_listing_text(url: str, limit: int = 6000) -> str:
    """Підтягнути читабельний текст сторінки оголошення (для режиму «за посиланням»)."""
    html = await _fetch_html(url)
    if not html:
        return ""
    body = html
    # беремо пріоритетно og:title/og:description — вони чисті й містять суть
    head_bits: list[str] = []
    for prop in ("og:title", "og:description", "description"):
        m = _re.search(
            r'<meta[^>]+(?:property|name)=["\']' + prop + r'["\'][^>]+content=["\']([^"\']+)',
            html, _re.I,
        )
        if m:
            head_bits.append(m.group(1))
    body = _SCRIPT_RE.sub(" ", body)
    body = _TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip()
    text = (" ".join(head_bits) + "\n" + body).strip()
    return text[:limit]


async def fetch_photos_as_images(url: str, limit: int = 8) -> list[tuple[bytes, str]]:
    """Підтягнути фото з посилання на оголошення і завантажити їх у байти.

    Використовуємо як «еталонні фото об'єкта», коли користувач дав не скриншоти,
    а текст оголошення з посиланням (режим пошуку за текстом).
    """
    photos = await fetch_listing_photos(url, limit=limit)
    if not photos:
        return []
    out: list[tuple[bytes, str]] = []
    async with httpx.AsyncClient() as client:
        downloaded = await asyncio.gather(*[_download_image(u, client) for u in photos])
    for d in downloaded:
        if d:
            out.append((d[0], d[1]))
    print(f"dubl ref photos {url[:60]}: fetched={len(photos)} downloaded={len(out)}", flush=True)
    return out


async def confirm_by_photo(object_images: list[tuple[bytes, str]], candidate: dict) -> dict:
    """Порівняти фото об'єкта зі скриншотів і фото кандидата через Gemini."""
    if not settings.gemini_api_key:
        return {"match": "no_key", "confidence": 0, "matched": 0}
    photos = await fetch_listing_photos(candidate.get("url") or "", limit=5)
    if not photos:
        return {"match": "no_photos", "confidence": 0, "matched": 0}

    import google.generativeai as genai
    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)

    content: list = [_PHOTO_PROMPT, "ФОТО ОБ'ЄКТА:"]
    for raw, mime in object_images[:4]:
        content.append({"mime_type": mime, "data": raw})
    content.append("ФОТО КАНДИДАТА:")
    got = 0
    async with httpx.AsyncClient() as client:
        downloaded = await asyncio.gather(*[_download_image(u, client) for u in photos])
    for d in downloaded:
        if d:
            content.append({"mime_type": d[1], "data": d[0]})
            got += 1
    if got == 0:
        print(f"dubl photo {(candidate.get('url') or '')[:70]}: 0 фото скачано", flush=True)
        return {"match": "no_photos", "confidence": 0, "matched": 0}

    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(model.generate_content, content, request_options={"timeout": 30}),
            timeout=45,
        )
        from dubl.extract import _parse_model_json
        data = _parse_model_json(resp.text)
        matched = int(data.get("matched_photos") or 0)
        conf = float(data.get("confidence") or 0)
        same = bool(data.get("same_object")) and conf >= 0.6
        print(f"dubl photo {(candidate.get('url') or '')[:60]}: got={got} "
              f"same={data.get('same_object')} matched={matched} conf={conf}", flush=True)
        return {
            "match": "yes" if same else "no",
            "confidence": conf,
            "matched": matched,
        }
    except Exception as error:
        print(f"dubl vip photo compare error: {type(error).__name__}: {error}", flush=True)
        return {"match": "error", "confidence": 0, "matched": 0}


# ---------------------------------------------------------------- інші портали (Serper site:)
# DOM.RIA через site-пошук (бо API на паузі) + rieltor/lun/m2bomber.
_EXTRA_PORTALS = ["dom.ria.com", "rieltor.ua", "lun.ua", "m2bomber.com"]


# Портали, оголошення яких беремо з веб-пошуку (Serper free забороняє site:,
# тому робимо звичайні запити і фільтруємо результати за доменом).
_WANTED_PORTALS = ["dom.ria.com", "rieltor.ua", "lun.ua", "m2bomber.com",
                   "flatfy.ua", "address.ua", "olx.ua"]

_RE_ROOMS = re.compile(r'(\d)\s*[-–]?\s*(?:кімнат|комнат|кімн|-?\s*к\b)', re.I)
_RE_AREA = re.compile(r'(\d{2,3}(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м)', re.I)
_RE_FLOOR = re.compile(r'(\d{1,2})\s*/\s*\d{1,2}', re.I)  # "2/5"


def _parse_card_text(text: str) -> tuple[int | None, float | None, int | None]:
    """Витягти кімнати / площу / поверх із заголовка+сніпета (для фільтра до фото)."""
    rooms = area = floor = None
    m = _RE_ROOMS.search(text or "")
    if m:
        try:
            rooms = int(m.group(1))
        except ValueError:
            pass
    m = _RE_AREA.search(text or "")
    if m:
        try:
            area = float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    m = _RE_FLOOR.search(text or "")
    if m:
        try:
            floor = int(m.group(1))
        except ValueError:
            pass
    return rooms, area, floor


async def search_extra_portals(subject: dict) -> list[dict]:
    """Оголошення на порталах через звичайний веб-пошук (без site:), фільтр за доменом.
    Запит по вулиці — ключ до пошуку САМЕ оригіналу. Підтвердження — по фото."""
    from dubl.catalog import serper_search, _domain
    city = subject.get("city") or ""
    district = subject.get("district") or ""
    street = subject.get("street") or ""
    rooms = subject.get("rooms")
    deal = "оренда" if str(subject.get("deal_type")) == "rent" else "продаж"
    rooms_part = f"{rooms}-кімнатна" if rooms else ""
    loc = street or district  # вулиця — найточніше; якщо немає — район
    area_part = ""
    try:
        if subject.get("area_sqm"):
            area_part = f"{round(float(subject['area_sqm']))} м²"
    except (TypeError, ValueError):
        pass

    queries = [
        " ".join(p for p in [deal, rooms_part, "квартира", city, loc, area_part] if p),
        " ".join(p for p in [deal, rooms_part, "квартира", city, loc] if p),
    ]
    out: dict[str, dict] = {}
    seen_q: set[str] = set()
    for query in queries:
        if not query or query in seen_q:
            continue
        seen_q.add(query)
        organic = await serper_search(query, num=30)
        for item in organic:
            url = item.get("link") or ""
            dom = _domain(url)
            portal = next((p for p in _WANTED_PORTALS if dom == p or dom.endswith("." + p)), None)
            if not portal or url in out or not _looks_like_listing(url):
                continue
            r_, a_, f_ = _parse_card_text((item.get("title") or "") + " " + (item.get("snippet") or ""))
            out[url] = {
                "source": "olx" if portal == "olx.ua" else portal,
                "url": url, "title": item.get("title") or "",
                "price_uah": None, "area_sqm": a_, "rooms": r_, "floor": f_,
            }
    return list(out.values())[:20]
