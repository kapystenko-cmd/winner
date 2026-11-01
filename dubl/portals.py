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


# ─────────────────────────────────────────────────────────────────────────
# ПОШУК ПО ПОРТАЛАХ напряму в їхніх розділах (продаж/оренда) з фільтром за
# містом/кімнатами/площею. Беремо вже відфільтровані оголошення з повними
# даними — щоб НЕ витрачати фото-перевірки на сміття.
#   • DOM.RIA — SSR-сторінка розділу містить window.__INITIAL_STATE__ з усіма
#     картками (ціна $/грн, площа, кімнати, поверх, вулиця) — без API/ZenRows.
#   • rieltor.ua — розділ /<місто>/flats-sale|flats-rent/<N>-rooms/ з картками.
# ─────────────────────────────────────────────────────────────────────────

# Перевірені слаги міст (той самий слаг працює і на DOM.RIA, і на rieltor).
# Решту резолвимо через веб-пошук. Хибний слаг відсікаємо звіркою назви міста.
_CITY_SLUGS = {
    "київ": "kiev", "харків": "kharkov", "одеса": "odessa", "дніпро": "dnepr",
    "львів": "lvov", "миколаїв": "nikolaev", "вінниця": "vinnitsa",
    "полтава": "poltava", "чернігів": "chernigov", "черкаси": "cherkassy",
    "житомир": "zhitomir", "суми": "sumy", "рівне": "rovno",
}


def _city_stem(name: str | None) -> str:
    """Корінь назви міста для звірки з урахуванням відмінків (Вінниця/Вінниці)."""
    w = _key(name).split(" ")[0]
    return w[:-2] if len(w) > 5 else w


def _city_matches(requested: str | None, got: str | None) -> bool:
    stem = _city_stem(requested)
    return bool(stem) and (stem in _key(got) or _city_stem(got) in _key(requested))


def _brace_match(html: str, i: int) -> str | None:
    """Повернути підрядок {…} зі збалансованими дужками, починаючи з позиції i."""
    if i >= len(html) or html[i] != "{":
        return None
    depth, instr, esc = 0, False, False
    for j in range(i, len(html)):
        ch = html[j]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[i:j + 1]
    return None


async def _get_text(url: str, client: httpx.AsyncClient, marker: str = "") -> str:
    """Отримати HTML розділу порталу. Спершу звичайний GET; якщо заблоковано
    (не 200 / немає маркера даних) — запасний шлях через ZenRows (без js_render,
    бо дані вже в SSR). На сервері прямий GET часто ріжуть — ZenRows рятує."""
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code == 200 and (not marker or marker in r.text):
            return r.text
        print(f"dubl portal plain {url[:55]} status={r.status_code} "
              f"marker={'y' if (marker and marker in (r.text or '')) else 'n'}", flush=True)
    except Exception as error:
        print(f"dubl portal get {url[:55]}: {type(error).__name__}", flush=True)
    if settings.zenrows_api_key:
        try:
            async with httpx.AsyncClient(timeout=50) as zc:
                r = await zc.get("https://api.zenrows.com/v1/", params={
                    "apikey": settings.zenrows_api_key, "url": url,
                })
                if r.status_code == 200 and (not marker or marker in r.text):
                    print(f"dubl portal zenrows OK {url[:55]}", flush=True)
                    return r.text
                print(f"dubl portal zenrows {url[:50]} status={r.status_code} "
                      f"marker={'y' if (marker and marker in (r.text or '')) else 'n'}", flush=True)
        except Exception as error:
            print(f"dubl portal zenrows {url[:50]}: {type(error).__name__}: {error}", flush=True)
    return ""


async def _resolve_slug(city: str, portal: str, client: httpx.AsyncClient) -> str:
    """Слаг міста для порталу: з мапи, інакше з веб-пошуку (перший URL порталу)."""
    slug = _CITY_SLUGS.get(_key(city))
    if slug:
        return slug
    try:
        from dubl.catalog import serper_search
        host = "dom.ria.com" if portal == "domria" else "rieltor.ua"
        organic = await serper_search(f"{host} квартири {city}", num=10)
        pat = (r'dom\.ria\.com/\w+/(?:prodazha|arenda|prodaja|orenda)-kvartir/([a-z0-9-]+?)/'
               if portal == "domria"
               else r'rieltor\.ua/([a-z0-9-]+?)/flats-(?:sale|rent)')
        for item in organic:
            m = re.search(pat, (item.get("link") or ""), re.I)
            if m:
                return m.group(1).lower()
        if portal == "domria":  # запасний варіант — зі слагу оголошення
            for item in organic:
                m = re.search(r'-kvartira-([a-z]+)-', (item.get("link") or ""), re.I)
                if m:
                    return m.group(1).lower()
    except Exception as error:
        print(f"dubl resolve slug {portal} {city}: {error}", flush=True)
    return ""


def _dimria_items(html: str) -> list[dict]:
    """Дістати список карток DOM.RIA з __INITIAL_STATE__. Перебираємо ВСІ
    присвоєння window.__INITIAL_STATE__ = … (бо ім'я трапляється і в іншому
    контексті) та обидві форми: об'єкт-літерал і JSON.parse("…")."""
    if not html:
        return []
    snippet = ""
    for m in re.finditer(r'window\.__INITIAL_STATE__\s*=\s*', html):
        pos = m.end()
        rest = html[pos:pos + 12]
        if not snippet:
            snippet = html[pos:pos + 60].replace("\n", " ")
        data = None
        if rest.lstrip().startswith("JSON.parse"):
            mm = re.match(r'\s*JSON\.parse\(\s*("(?:[^"\\]|\\.)*")\s*\)', html[pos:pos + 2_000_000], re.S)
            if mm:
                try:
                    data = json.loads(json.loads(mm.group(1)))
                except Exception:
                    data = None
        elif rest.lstrip()[:1] == "{":
            start = pos + (len(rest) - len(rest.lstrip()))
            braced = _brace_match(html, start)
            if braced:
                try:
                    data = json.loads(braced)
                except Exception:
                    data = None
        if data:
            items = (((data or {}).get("catalog") or {}).get("realtyForCatalog")) or []
            if items:
                return items
    print(f"dubl dimria parse fail: len={len(html)} "
          f"has_catalog={'realtyForCatalog' in html} snippet={snippet!r}", flush=True)
    return []


async def search_dimria(subject: dict, pages: int = 3) -> list[dict]:
    """DOM.RIA напряму: розділ продажу/оренди квартир міста → картки з JSON."""
    city = subject.get("city")
    otype = str(subject.get("object_type") or "apartment")
    if not city or otype != "apartment":  # прямий скрейп — лише квартири
        return []
    rooms = _as_int(subject.get("rooms"))
    try:
        area = float(subject["area_sqm"]) if subject.get("area_sqm") not in (None, "") else None
    except (TypeError, ValueError):
        area = None
    deal = "arenda-kvartir" if str(subject.get("deal_type")) == "rent" else "prodazha-kvartir"
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=30, headers=_HDRS) as client:
            slug = await _resolve_slug(city, "domria", client)
            if not slug:
                print(f"dubl dimria: slug не знайдено для {city}", flush=True)
                return []
            base = f"https://dom.ria.com/uk/{deal}/{slug}/"
            htmls = await asyncio.gather(*[
                _get_text(base if p == 1 else base + f"?page={p}", client,
                          marker="__INITIAL_STATE__")
                for p in range(1, pages + 1)
            ])
            validated = None
            for html in htmls:
                if not html:
                    continue
                items = _dimria_items(html)
                for it in items:
                    got_city = it.get("city_name_uk") or it.get("cityName") or ""
                    if validated is None:
                        validated = _city_matches(city, got_city)
                        if not validated:
                            print(f"dubl dimria: слаг {slug} дав інше місто ({got_city}) — пропуск", flush=True)
                            return []
                    bu = it.get("beautiful_url") or ""
                    if not bu:
                        continue
                    price_usd = _num(it.get("priceUSD")) or _num((it.get("priceArr") or {}).get("1"))
                    price_uah = _num(it.get("priceUAH")) or _num((it.get("priceArr") or {}).get("3"))
                    street = it.get("street_name_uk") or it.get("streetName") or ""
                    building = it.get("building_number_str") or ""
                    out.append({
                        "source": "dimria",
                        "url": "https://dom.ria.com/uk/" + bu,
                        "title": (it.get("description_uk") or "")[:120],
                        "price_usd": round(price_usd) if price_usd else None,
                        "price_uah": round(price_uah) if price_uah else None,
                        "area_sqm": _num(it.get("total_square_meters")) or None,
                        "rooms": _as_int(it.get("rooms_count")),
                        "floor": _as_int(it.get("floor")),
                        "street": street,
                        "address": (str(street) + " " + str(building)).strip(),
                        "city": got_city,
                    })
    except Exception as error:
        print(f"dubl dimria error: {type(error).__name__}: {error}", flush=True)
        return []
    tightened = _tighten(out, rooms, area)
    print(f"dubl dimria {slug}/{deal}: усього {len(out)} → після фільтра {len(tightened)}", flush=True)
    return tightened


_RI_FLOOR_RE = re.compile(r'поверх\s*(\d{1,2})\s*з\s*\d{1,2}', re.I)
_RI_ROOMS_AREA_RE = re.compile(r'(\d)\s*кімнат\w*\s*(\d{2,3}(?:[.,]\d)?)\s*/\s*\d', re.I)
_RI_PRICE_RE = re.compile(r'(\d[\d\s  ]{2,})\s*\$')


async def search_rieltor(subject: dict) -> list[dict]:
    """rieltor.ua напряму: розділ продажу/оренди (+фільтр кімнат у шляху)."""
    city = subject.get("city")
    otype = str(subject.get("object_type") or "apartment")
    if not city or otype != "apartment":
        return []
    rooms = _as_int(subject.get("rooms"))
    try:
        area = float(subject["area_sqm"]) if subject.get("area_sqm") not in (None, "") else None
    except (TypeError, ValueError):
        area = None
    deal = "flats-rent" if str(subject.get("deal_type")) == "rent" else "flats-sale"
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=30, headers=_HDRS) as client:
            slug = await _resolve_slug(city, "rieltor", client)
            if not slug:
                print(f"dubl rieltor: slug не знайдено для {city}", flush=True)
                return []
            room_path = f"{rooms}-rooms/" if rooms else ""
            url = f"https://rieltor.ua/{slug}/{deal}/{room_path}"
            html = await _get_text(url, client, marker=f"/{deal}/view/")
            if not html:
                return []
            # Звірка міста: більшість карток мають слаг саме цього міста.
            page_slugs = re.findall(rf'rieltor\.ua/([a-z0-9-]+)/{deal}/view/', html, re.I)
            if page_slugs:
                from collections import Counter
                top = Counter(page_slugs).most_common(1)[0][0]
                if top != slug:
                    print(f"dubl rieltor: слаг {slug} дав інше місто ({top}) — пропуск", flush=True)
                    return []
            # Картки в порядку DOM: id ↔ (кімнати,площа) ↔ поверх ↔ ціна.
            ids: list[str] = []
            for m in re.finditer(rf'/{deal}/view/(\d+)/', html):
                if not ids or ids[-1] != m.group(1):
                    ids.append(m.group(1))
            # Площа/кімнати/поверх беремо з тексту карток (надійно вирівняні за
            # порядком DOM). Ціну НЕ парсимо з тексту (там ще й ціна за м² збиває
            # вирівнювання) — її дотягнемо зі сторінки показаних кандидатів.
            plain = re.sub(r"<[^>]+>", " ", html)
            ra = _RI_ROOMS_AREA_RE.findall(plain)
            floors = _RI_FLOOR_RE.findall(plain)
            for i, cid in enumerate(ids):
                r_ = _as_int(ra[i][0]) if i < len(ra) else rooms
                a_ = None
                if i < len(ra):
                    try:
                        a_ = float(ra[i][1].replace(",", "."))
                    except ValueError:
                        a_ = None
                f_ = _as_int(floors[i]) if i < len(floors) else None
                out.append({
                    "source": "rieltor.ua",
                    "url": f"https://rieltor.ua/{slug}/{deal}/view/{cid}/",
                    "title": "",
                    "price_usd": None,
                    "price_uah": None,
                    "area_sqm": a_,
                    "rooms": r_ or rooms,
                    "floor": f_,
                    "street": "",
                    "address": "",
                    "city": city,
                })
    except Exception as error:
        print(f"dubl rieltor error: {type(error).__name__}: {error}", flush=True)
        return []
    tightened = _tighten(out, rooms, area)
    print(f"dubl rieltor {slug}/{deal}: усього {len(out)} → після фільтра {len(tightened)}", flush=True)
    return tightened


def _tighten(items: list[dict], rooms: int | None, area: float | None) -> list[dict]:
    """Строгий відбір на рівні джерела: кімнати точно, площа ±10%."""
    tol = settings.area_tolerance
    out = []
    for c in items:
        cr = _as_int(c.get("rooms"))
        if rooms and cr and cr != rooms:
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


_CATEGORY_MARKERS = (
    "q-", "/list/", "/search", "/filter", "/poshuk", "/poisk",
    "/flats-sale", "/flats-rent", "/prodazha-kvartir", "/prodaja-kvartir",
    "/arenda-kvartir", "/orenda-kvartir", "-rooms/", "/sale/", "/rent/",
    "/flats?", "/flats-", "-rayon-po", "/nedvizhimost", "/kvartiry", "/catalog",
)


def _looks_like_listing(url: str) -> bool:
    """Тільки окремі оголошення (не категорії/пошук/фільтри).
    Листинг = є маркер оголошення або довгий числовий id у шляху."""
    low = (url or "").lower().split("?")[0]
    if any(b in low for b in _CATEGORY_MARKERS):
        return False
    if "obyavlenie" in low or low.endswith(".html"):
        return True
    # окреме оголошення зазвичай має довгий числовий id (6+ цифр) у шляху
    return bool(re.search(r"\d{6,}", low))


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
    if str(subject.get("deal_type")) == "rent":
        # Перевірений olx_service ocinka працює лише на ПРОДАЖ (аренду відсіює).
        # Для оренди OLX поки пропускаємо — покривають DOM.RIA і rieltor.
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


_OG_PRICE_AMOUNT_RE = _re.compile(r'og:price:amount["\'][^>]+content=["\']([\d.]+)', _re.I)
_OG_PRICE_CUR_RE = _re.compile(r'og:price:currency["\'][^>]+content=["\']([A-Za-z]{3})', _re.I)
_JSONLD_PRICE_RE = _re.compile(r'"price"\s*:\s*"?(\d[\d.]*)"?', _re.I)
_JSONLD_CUR_RE = _re.compile(r'"priceCurrency"\s*:\s*"?([A-Za-z]{3})', _re.I)
_USD_TEXT_RE = _re.compile(r'(\d[\d\s  ]{2,})\s*(?:\$|USD|дол)', _re.I)
_UAH_TEXT_RE = _re.compile(r'(\d[\d\s  ]{4,})\s*(?:грн|₴|UAH)', _re.I)


def _clean_amount(raw: str) -> float | None:
    try:
        v = float(_re.sub(r"[^\d.]", "", (raw or "").replace(",", ".").split(".")[0]) or 0)
    except ValueError:
        return None
    return v if 500 <= v <= 50_000_000 else None


def _parse_price(html: str) -> tuple[float | None, float | None]:
    """Витягти ціну зі сторінки → (usd, uah). Пріоритет: og:price / JSON-LD / текст."""
    usd = uah = None
    m_amt = _OG_PRICE_AMOUNT_RE.search(html) or _JSONLD_PRICE_RE.search(html)
    if m_amt:
        cur_m = _OG_PRICE_CUR_RE.search(html) or _JSONLD_CUR_RE.search(html)
        cur = (cur_m.group(1).upper() if cur_m else "")
        val = _clean_amount(m_amt.group(1))
        if val:
            if cur == "USD":
                usd = val
            elif cur == "UAH":
                uah = val
            elif val < 1_000_000:  # без валюти і невелике — найімовірніше $
                usd = val
            else:
                uah = val
    if usd is None:
        for m in _USD_TEXT_RE.finditer(html):
            v = _clean_amount(m.group(1))
            if v and v < 2_000_000:
                usd = v
                break
    if uah is None:
        for m in _UAH_TEXT_RE.finditer(html):
            v = _clean_amount(m.group(1))
            if v and v >= 100_000:
                uah = v
                break
    return usd, uah


async def enrich_candidate_prices(subject: dict, candidates: list[dict]) -> None:
    """Дотягнути ціну зі сторінок кандидатів, у яких її немає (фінальний шорт-лист)."""
    want_usd = str(subject.get("price_currency") or "").upper() == "USD"
    rate = await usd_rate() if want_usd else 0.0
    targets = [c for c in candidates if not c.get("price_uah") and not c.get("price_usd")][:8]
    if not targets:
        return

    async def one(c: dict) -> None:
        html = await _fetch_html(c.get("url") or "")
        if not html:
            return
        usd, uah = _parse_price(html)
        if usd and not c.get("price_usd"):
            c["price_usd"] = round(usd)
        if uah and not c.get("price_uah"):
            c["price_uah"] = round(uah)
        if want_usd and not c.get("price_usd") and c.get("price_uah") and rate:
            try:
                c["price_usd"] = round(float(c["price_uah"]) / rate)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        print(f"dubl price {(c.get('url') or '')[:55]}: usd={c.get('price_usd')} uah={c.get('price_uah')}", flush=True)

    sem = asyncio.Semaphore(4)

    async def guarded(c):
        async with sem:
            try:
                await one(c)
            except Exception as error:
                print(f"dubl price error: {error}", flush=True)

    await asyncio.gather(*[guarded(c) for c in targets])


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
# DOM.RIA / rieltor / OLX шукаємо НАПРЯМУ (вище), тож тут лише решта агрегаторів.
_WANTED_PORTALS = ["lun.ua", "m2bomber.com", "flatfy.ua", "address.ua"]

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
    """Оголошення того ж об'єкта через звичайний веб-пошук (Google/Serper).
    Беремо і портали, і САЙТИ АГЕНТСТВ (перші органічні результати, не рекламу).
    Запит по вулиці + площі + ціні — ключ до пошуку САМЕ цього об'єкта."""
    from dubl.catalog import serper_search, _domain, _is_agency_domain
    city = subject.get("city") or ""
    district = subject.get("district") or ""
    street = subject.get("street") or ""
    building = subject.get("building") or ""
    rooms = subject.get("rooms")
    deal = "оренда" if str(subject.get("deal_type")) == "rent" else "продаж"
    rooms_part = f"{rooms}-кімнатна" if rooms else ""
    loc = street or district  # вулиця — найточніше; якщо немає — район
    street_full = (str(street) + " " + str(building)).strip() if street else ""
    area_part = ""
    try:
        if subject.get("area_sqm"):
            area_part = f"{round(float(subject['area_sqm']))} м²"
    except (TypeError, ValueError):
        pass
    price_part = ""
    cur = str(subject.get("price_currency") or "").upper()
    if subject.get("price_value") and cur:
        price_part = f"{subject['price_value']}$" if cur == "USD" else f"{subject['price_value']} грн"

    # Різні формулювання — так Google віддає різні джерела (портали + агентства).
    queries = [
        " ".join(p for p in [deal, rooms_part, "квартира", city, loc, area_part] if p),
        " ".join(p for p in [deal, rooms_part, "квартира", city, street_full or loc, price_part] if p),
        " ".join(p for p in [rooms_part, "квартира", city, loc, area_part, price_part] if p),
        " ".join(p for p in [deal, rooms_part, "квартира", city, loc] if p),
    ]

    out: dict[str, dict] = {}
    agency_count = 0
    MAX_AGENCY = 10
    seen_q: set[str] = set()
    for query in queries:
        if not query or query in seen_q:
            continue
        seen_q.add(query)
        organic = await serper_search(query, num=30)
        for item in organic:
            url = item.get("link") or ""
            dom = _domain(url)
            if not url or url in out or not _looks_like_listing(url):
                continue
            portal = next((p for p in _WANTED_PORTALS if dom == p or dom.endswith("." + p)), None)
            if portal:
                src = "olx" if portal == "olx.ua" else portal
            elif _is_agency_domain(dom) and agency_count < MAX_AGENCY:
                # сайт агентства — теж може містити той самий об'єкт
                src = dom
                agency_count += 1
            else:
                continue
            r_, a_, f_ = _parse_card_text((item.get("title") or "") + " " + (item.get("snippet") or ""))
            out[url] = {
                "source": src,
                "url": url, "title": item.get("title") or "",
                "price_uah": None, "area_sqm": a_, "rooms": r_, "floor": f_,
            }
    return list(out.values())[:30]
