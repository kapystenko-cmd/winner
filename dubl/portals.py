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
    if not settings.dimria_api_key or not settings.dimria_enabled:
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
