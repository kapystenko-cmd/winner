"""DIM.RIA API search and listing evidence capture.

The API returns only listing identifiers from ``/dom/search``.  We then load
each selected card through ``/dom/info/{id}``, so the scoring module receives
the same normalized fields from DIM.RIA and OLX.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings


DIMRIA_BASE = "https://developers.ria.com/dom"
# The DIM.RIA WAF returned an anti-bot challenge page ("Ваш запрос попал в
# категорию подозрительных") instead of JSON on a real run — httpx's default
# User-Agent ("python-httpx/0.2x") is an instant script fingerprint. A
# realistic browser header is the standard, low-risk fix; it does not change
# what data is requested, only how the client identifies itself.
_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
}
_DETAIL_CONCURRENCY = 2
_city_cache: dict[str, tuple[int | None, int]] = {}
_city_cache_loaded = False


def _safe_json(response: httpx.Response, context: str) -> Any:
    """Parse a DIM.RIA JSON response with a diagnosable failure.

    A previous version let response.json() raise json.JSONDecodeError
    ("Expecting value: line 2 column 1 (char 1)") with no visibility into
    WHICH call failed or what the API actually sent back — a quota block,
    an auth error, and a genuine outage all look identical from that message
    alone. Print the status code and a body snippet before the caller's
    broad except swallows it, so the next real run tells us which one it is.
    """
    try:
        return response.json()
    except Exception as error:
        snippet = response.text[:300] if response.text else "(порожнє тіло)"
        print(f"DIM.RIA bad JSON at {context}: status={response.status_code}, body={snippet!r}")
        raise

# The apartment values and characteristic IDs are those documented by
# Developers.RIA.  Houses and land are searched by their parent category; this
# avoids silently restricting the search to only one subtype (e.g. dachas).
_PROPERTY_SEARCH = {
    "apartment": {"category": 1, "realty_type": 2, "area_characteristic": 214, "rooms_characteristic": 209},
    "house": {"category": 4, "area_characteristic": 215, "rooms_characteristic": 209},
    "land": {"category": 5, "area_characteristic": 219},
}


def _key(value: str | None) -> str:
    """Reduce a place name to a comparable key.

    Both sides of every comparison go through this, so it must produce the
    same key for "Чернігівська", "Чернігівська обл." and "Чернігівська
    область" -- the OCR layer returns whichever form the document used, while
    the DIM.RIA API always returns the bare name.

    Two bugs lived here before, both of the same kind: a leftover character
    that made the keys differ invisibly. The trailing space after removing
    "область", and then a trailing "." after "обл." -- the \\b in the pattern
    below cannot anchor after a period at end of string, so the engine
    backtracked to matching bare "обл" and left the dot behind. A mismatched
    region key is not a harmless miss: the caller then scans every oblast in
    turn, which is exactly what triggers the API's 429 rate limit.
    Normalising punctuation and whitespace at the end covers both, and any
    further variant of the same shape.
    """
    value = str(value or "").casefold().strip()
    value = re.sub(r"^(м\.?|місто|г\.?|город)\s*", "", value)
    value = re.sub(r"\b(область|обл|район|р-н)\b\.?", "", value)
    value = value.replace("’", "'").replace("-", " ")
    # Collapse runs of whitespace and drop stray leading/trailing punctuation
    # left behind by the removals above.
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .,;:").strip()


def _load_city_cache() -> None:
    """Keep resolved DIM.RIA city IDs between API restarts.

    The official DIM.RIA city endpoint is scoped to an oblast.  Without a
    cache, an unfamiliar city can require checking many oblasts again after
    every service restart, spending quota before the real listing search.
    """
    global _city_cache_loaded
    if _city_cache_loaded:
        return
    _city_cache_loaded = True
    try:
        path = Path(settings.logs_dir) / "dimria_city_cache.json"
        saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        for key, value in saved.items():
            if isinstance(value, list) and len(value) == 2:
                _city_cache[key] = (int(value[0]) if value[0] else None, int(value[1]))
    except Exception as error:
        print("DIM.RIA city-cache read error: " + str(error))


def _save_city_cache() -> None:
    try:
        path = Path(settings.logs_dir) / "dimria_city_cache.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(_city_cache, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except Exception as error:
        print("DIM.RIA city-cache write error: " + str(error))


async def _resolve_city(city: str, client: httpx.AsyncClient, region: str | None = None) -> tuple[int | None, int] | None:
    """Resolve any Ukrainian city through DIM.RIA, then cache it in memory."""
    wanted = _key(city)
    if not wanted:
        return None
    wanted_region = _key(region)
    cache_key = wanted + ("|" + wanted_region if wanted_region else "")
    _load_city_cache()
    if cache_key in _city_cache:
        return _city_cache[cache_key]

    states_response = await client.get(
        DIMRIA_BASE + "/states", params={"api_key": settings.dimria_api_key, "lang_id": 4}
    )
    states_response.raise_for_status()
    states = _safe_json(states_response, "GET /states") or []

    # A region extracted from the legal/technical documents narrows the
    # official city lookup to one oblast, instead of consuming a call for
    # every oblast.  If it is absent or not recognised, the broad fallback
    # below still supports every Ukrainian city.
    region_states = []
    if wanted_region:
        region_states = [state for state in states if wanted_region in {
            _key(state.get("name")), _key(state.get("declension")),
            _key(state.get("region_name")), _key(state.get("center_declension")),
        }]

    # First resolve oblast centres without city-list requests.
    for state in states:
        aliases = (state.get("region_name"), state.get("center_declension"), state.get("name"))
        if wanted in {_key(alias) for alias in aliases if alias}:
            state_id = int(state["stateID"])
            _city_cache[cache_key] = (state_id, state_id)
            _save_city_cache()
            return _city_cache[cache_key]

    # Other cities are obtained from the official cities/:stateId endpoint.
    # Requests are bounded and only happen once per previously unseen city.
    semaphore = asyncio.Semaphore(5)

    async def find_in_state(state: dict[str, Any]) -> tuple[int | None, int] | None:
        async with semaphore:
            state_id = int(state["stateID"])
            response = await client.get(
                DIMRIA_BASE + f"/cities/{state_id}",
                params={"api_key": settings.dimria_api_key, "lang_id": 4},
            )
            response.raise_for_status()
            for item in _safe_json(response, f"GET /cities/{state_id}") or []:
                if wanted == _key(item.get("name")):
                    return state_id, int(item["cityID"])
        return None

    states_to_check = region_states or states
    tasks = [asyncio.create_task(find_in_state(state)) for state in states_to_check]
    try:
        for task in asyncio.as_completed(tasks):
            found = await task
            if found:
                _city_cache[cache_key] = found
                _save_city_cache()
                return found
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
    return None


def _as_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9,.-]", "", str(value)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


async def search_analogs(
    city: str,
    property_type: str = "apartment",
    rooms: int | None = None,
    area_sqm: float | None = None,
    district: str | None = None,
    region: str | None = None,
    max_results: int = 30,
) -> list[dict[str, Any]]:
    """Get a market sample from DIM.RIA using sale and area/room filters."""
    if not settings.dimria_api_key:
        print("DIM.RIA: API key is not configured")
        return []

    spec = _PROPERTY_SEARCH.get(property_type)
    if not spec:
        print(f"DIM.RIA: unsupported property type {property_type}")
        return []

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=_REQUEST_HEADERS) as client:
            resolved = await _resolve_city(city, client, region=region)
            if not resolved:
                print(f"DIM.RIA: city was not resolved: {city}")
                return []
            state_id, city_id = resolved
            params: dict[str, Any] = {
                "api_key": settings.dimria_api_key,
                "category": spec["category"],
                "operation_type": 1,  # sale
                "city_id": city_id,
                "page": 0,
                "limit": min(max(1, max_results), 60),
            }
            if state_id:
                params["state_id"] = state_id
            if spec.get("realty_type"):
                params["realty_type"] = spec["realty_type"]
            # Area is intentionally not passed as an API filter here (product
            # decision): only room count is enforced strictly for DIM.RIA too,
            # matching OLX. Area is still returned per-listing and used by
            # valuation_analytics for weighting/segment placement — it just
            # no longer narrows the search itself.
            if rooms and spec.get("rooms_characteristic"):
                params[f"characteristic[{spec['rooms_characteristic']}][from]"] = int(rooms)
                params[f"characteristic[{spec['rooms_characteristic']}][to]"] = int(rooms)

            response = await client.get(DIMRIA_BASE + "/search", params=params)
            response.raise_for_status()
            payload = _safe_json(response, "GET /search") or {}
            item_ids = list(payload.get("items") or [])[:max_results]
            print(f"DIM.RIA search: city={city}, ids={len(item_ids)}, total={payload.get('count', 0)}")

            # Cascade for houses/land: widen to oblast if city gave < 5
            if property_type in ("house", "land") and len(item_ids) < 5 and state_id:
                print(f"DIM.RIA cascade: city gave {len(item_ids)}, widening to oblast (state_id={state_id})")
                oblast_params = dict(params)
                oblast_params.pop("city_id", None)
                oblast_resp = await client.get(DIMRIA_BASE + "/search", params=oblast_params)
                oblast_resp.raise_for_status()
                oblast_payload = _safe_json(oblast_resp, "GET /search oblast") or {}
                oblast_ids = list(oblast_payload.get("items") or [])[:max_results]
                print(f"DIM.RIA cascade oblast: {len(oblast_ids)} ids")
                seen = set(str(i) for i in item_ids)
                for oid in oblast_ids:
                    if str(oid) not in seen:
                        item_ids.append(oid)
                        seen.add(str(oid))
                item_ids = item_ids[:max_results]

            # Sequential detail loading with delay to avoid DIM.RIA burst
            # rate limit (429). Parallel gather with semaphore still fires
            # requests too close together when 429 errors return instantly.
            details = []
            for item_id in item_ids:
                detail = await _get_detail(item_id, client)
                details.append(detail)
                await asyncio.sleep(0.6)

            return [item for item in details if item]
    except Exception as error:
        # DIM.RIA outages/quota errors never cancel an optional OLX search.
        print("DIM.RIA search error: " + str(error))
        return []


async def _get_detail(item_id: int | str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    try:
        response = await client.get(
            DIMRIA_BASE + "/info/" + str(item_id),
            params={"api_key": settings.dimria_api_key, "lang_id": 4},
        )
        response.raise_for_status()
        data = _safe_json(response, f"GET /info/{item_id}") or {}

        # API examples return UAH as priceArr["3"].  Never interpret the USD
        # price in priceArr["1"] as hryvnias.
        price_arr = data.get("priceArr") or {}
        price_uah = _as_number(price_arr.get("3"))
        if not price_uah and str(data.get("currency_type") or "").casefold() in {"грн", "uah"}:
            price_uah = _as_number(data.get("price_total") or data.get("price"))
        if not price_uah:
            from app.services.olx_service import get_usd_rate
            price_uah = _as_number(data.get("price_total") or data.get("price")) * await get_usd_rate()

        street = data.get("street_name_uk") or data.get("street_name") or ""
        building = data.get("building_number_str") or ""
        listing_slug = str(data.get("beautiful_url") or item_id)
        if not listing_slug.endswith(".html"):
            listing_slug += ".html"
        return {
            "source": "dimria",
            "source_id": str(item_id),
            "url": "https://dom.ria.com/uk/" + listing_slug,
            "title": data.get("description_uk") or data.get("description") or data.get("realty_type_name") or "",
            "price_uah": round(price_uah),
            "area_sqm": _as_number(data.get("total_square_meters")),
            "rooms": data.get("rooms_count"),
            "floor": data.get("floor"),
            "total_floors": data.get("floors_count"),
            "address": (str(street) + " " + str(building)).strip(),
            "city": data.get("city_name_uk") or data.get("city_name") or "",
            "district": data.get("district_name_uk") or data.get("district_name") or data.get("admin_district_name_uk") or "",
            "raw_data": data,
        }
    except Exception as error:
        print(f"DIM.RIA detail error for {item_id}: {error}")
        return None


async def take_screenshot(url: str, save_path: str) -> bool:
    """Capture one DIM.RIA listing card for inclusion in a report.

    Pipeline (one simple path, no Gemini, no dead crop import):
      Local Playwright with zoom 0.5 at 900x1600 ->
      ZenRows screenshot endpoint as fallback (JPEG, zoom 0.5, 900x1600) ->
      ScraperAPI as last resort.
    Percentage crop is applied once, centrally, in report_generator.py.
    """
    import os
    from pathlib import Path

    from app.services.browser_screenshot_service import take_browser_screenshots
    # Local Chromium first — unlike OLX, DIM.RIA does not block the server IP.
    if await take_browser_screenshots(url, save_path, single_frame=True, extra_wait_ms=2500):
        return True

    if settings.zenrows_api_key:
        hide_css = (
            "var css="
            "'body { zoom: 0.5 !important; } "
            "[data-testid=\"cookies-bar\"],[data-testid*=\"cookie\"],"
            "[class*=\"cookie\"],[class*=\"Cookie\"],[id*=\"cookie\"],"
            "[class*=\"consent\"],[class*=\"Consent\"],[id*=\"consent\"],"
            "[class*=\"gdpr\"],[class*=\"Gdpr\"]"
            "{display:none !important;visibility:hidden !important;height:0 !important;}';"
            "var s=document.createElement('style');"
            "s.innerHTML=css;document.head.appendChild(s);"
        )
        # Viewport reverted to 900x1600. OLX log showed ZenRows ignoring
        # window_width when it had to bypass anti-bot; stay on the known-
        # working size here. DIM.RIA's primary path is local Playwright
        # (which does use 1800x2160), this is only the fallback.
        try:
            async with httpx.AsyncClient(timeout=60, headers=_REQUEST_HEADERS) as client:
                response = await client.get("https://api.zenrows.com/v1/", params={
                    "apikey": settings.zenrows_api_key, "url": url,
                    "screenshot": "true",
                    "screenshot_format": "jpeg",
                    "screenshot_quality": 92,
                    "js_render": "true", "wait": 800,
                    "window_width": 900, "window_height": 1600,
                    "device": "desktop",
                    "js_instructions": json.dumps([
                        {"wait": 1200},
                        {"evaluate": hide_css},
                        {"wait": 1800},
                    ]),
                })
                response.raise_for_status()
                Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                Path(save_path).write_bytes(response.content)
                print("DIM.RIA screenshot created through ZenRows: " + url)
                return True
        except httpx.HTTPStatusError as error:
            body_snippet = ""
            try:
                body_snippet = error.response.text[:300]
            except Exception:
                pass
            print(f"Screenshot ZR error: HTTP {error.response.status_code} for url={error.request.url} body={body_snippet!r}")
        except Exception as error:
            print(f"Screenshot ZR error: {type(error).__name__}: {error!r}")

    scraper_key = getattr(settings, "scraper_api_key", "") or os.environ.get("SCRAPER_API_KEY", "")
    if scraper_key and getattr(settings, "scraperapi_enabled", True):
        try:
            async with httpx.AsyncClient(timeout=60, headers=_REQUEST_HEADERS) as client:
                response = await client.get("https://api.scraperapi.com", params={"api_key": scraper_key, "url": url, "screenshot": "true"})
                response.raise_for_status()
                screenshot_url = response.headers.get("sa-screenshot")
                if not screenshot_url:
                    raise RuntimeError("ScraperAPI did not return sa-screenshot header")
                image = await client.get(screenshot_url)
                image.raise_for_status()
                Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                Path(save_path).write_bytes(image.content)
                return True
        except Exception as error:
            print("Screenshot error: " + str(error))

    return False
