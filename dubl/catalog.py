"""M2: обнаружение агентств нерухомості по місту (широкий пошук).

Три джерела, усі паралельно, з багатьма запитами й пагінацією:
  • Serper.dev — веб-пошук Google (основний драйвер обсягу);
  • Google Places API (New) — карта агентств (+ сайт/телефон);
  • OpenStreetMap Overpass — office=estate_agent.

Повертаємо ЛИШЕ посилання/контакти. Нічого тут не зберігаємо на диск —
каталог-сховище підключимо окремим кроком (щоб не шукати щоразу з нуля).
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

from dubl.config import settings

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SERPER_URL = "https://google.serper.dev/search"
_UA = {"User-Agent": "dubl.top/0.1 (real-estate duplicate finder)"}

# Великі портали, агрегатори, соцмережі, ЗМІ, каталоги — це НЕ сайти агентств
# (портали обходимо окремо на етапі зрізу цін).
_NON_AGENCY_DOMAINS = {
    # портали нерухомості
    "olx.ua", "dom.ria.com", "ria.com", "lun.ua", "rieltor.ua", "m2bomber.com",
    "flatfy.ua", "address.ua", "realt.ua", "realtua.com", "novobudovy.com",
    "blizko.ua", "slando.ua",
    # маркетплейси/каталоги
    "prom.ua", "ua.all.biz", "hotline.ua", "zakupki.prom.ua", "satu.kz",
    # соцмережі / відео / месенджери
    "facebook.com", "instagram.com", "youtube.com", "t.me", "telegram.me",
    "tiktok.com", "twitter.com", "x.com", "linkedin.com", "pinterest.com",
    # карти / довідники
    "google.com", "maps.google.com", "2gis.ua", "waze.com",
    # енциклопедії / держава
    "wikipedia.org", "zakon.rada.gov.ua", "gov.ua",
    # ЗМІ / робота
    "work.ua", "robota.ua", "ukr.net", "i.ua", "bigmir.net", "unian.ua",
    "obozrevatel.com", "pravda.com.ua", "rbc.ua", "liga.net", "minfin.com.ua",
    "finance.ua",
}

# Багато варіантів запитів — так веб-пошук віддає різні агентства.
_SERPER_QUERIES = [
    "агентство нерухомості {city}",
    "агенція нерухомості {city}",
    "ріелтор {city}",
    "АН нерухомості {city}",
    "нерухомість {city} продаж квартир агентство",
    "нерухомість {city} оренда квартир агентство",
    "продаж будинків {city} агентство нерухомості",
    "новобудови {city} агентство нерухомості",
    "елітна нерухомість {city} агентство",
    "комерційна нерухомість {city} агентство",
    "ріелторські послуги {city}",
    "купити квартиру {city} агентство нерухомості",
    "real estate agency {city}",
    "real estate {city} Ukraine",
]
_PLACES_QUERIES = [
    "агентство нерухомості {city}",
    "агенція нерухомості {city}",
    "ріелтор {city}",
    "нерухомість продаж оренда {city}",
]


def _domain(url: str | None) -> str:
    try:
        net = urlparse(url or "").netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


def _is_agency_domain(dom: str) -> bool:
    if not dom or "." not in dom:
        return False
    # відсікаємо і сам домен, і будь-який його піддомен
    for bad in _NON_AGENCY_DOMAINS:
        if dom == bad or dom.endswith("." + bad):
            return False
    return True


# ---------------------------------------------------------------- Serper
async def find_agencies_serper(city: str) -> list[dict]:
    key = settings.serper_api_key
    if not key:
        return []
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    semaphore = asyncio.Semaphore(8)

    async def one(client: httpx.AsyncClient, query: str, page: int) -> list[dict]:
        async with semaphore:
            try:
                resp = await client.post(SERPER_URL, headers=headers, json={
                    "q": query, "gl": "ua", "hl": "uk", "num": 100, "page": page,
                })
                resp.raise_for_status()
                return resp.json().get("organic") or []
            except Exception as error:
                print(f"dubl catalog serper error ({query} p{page}): {error}", flush=True)
                return []

    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=25) as client:
        tasks = []
        for template in _SERPER_QUERIES:
            query = template.format(city=city)
            for page in range(1, 5):  # сторінки 1–4 (глибше Google майже не віддає)
                tasks.append(one(client, query, page))
        for organic in await asyncio.gather(*tasks):
            for item in organic:
                dom = _domain(item.get("link"))
                if not _is_agency_domain(dom) or dom in out:
                    continue
                out[dom] = {
                    "name": item.get("title") or dom,
                    "website": "https://" + dom,
                    "phone": "",
                    "address": "",
                    "source": "serper",
                }
    return list(out.values())


# ---------------------------------------------------------------- Places
async def find_agencies_places(city: str) -> list[dict]:
    key = settings.google_places_api_key
    if not key:
        return []
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": (
            "places.displayName,places.websiteUri,places.nationalPhoneNumber,"
            "places.formattedAddress,places.id,nextPageToken"
        ),
    }

    async def query_all_pages(client: httpx.AsyncClient, query: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        token: str | None = None
        for _ in range(3):  # до 3 сторінок = 60 місць на запит
            body: dict = {"textQuery": query, "languageCode": "uk", "regionCode": "UA", "pageSize": 20}
            if token:
                body["pageToken"] = token
            try:
                resp = await client.post(PLACES_URL, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            except Exception as error:
                body_snippet = ""
                try:
                    body_snippet = error.response.text[:200]  # type: ignore[attr-defined]
                except Exception:
                    pass
                print(f"dubl catalog places error ({query}): {error} {body_snippet}", flush=True)
                break
            for place in data.get("places") or []:
                name = (place.get("displayName") or {}).get("text") or ""
                website = place.get("websiteUri") or ""
                kid = _domain(website) or place.get("id") or name
                if not kid or kid in out:
                    continue
                out[kid] = {
                    "name": name,
                    "website": website,
                    "phone": place.get("nationalPhoneNumber") or "",
                    "address": place.get("formattedAddress") or "",
                    "source": "places",
                }
            token = data.get("nextPageToken")
            if not token:
                break
            await asyncio.sleep(2)  # New API: токен наступної сторінки стає чинним за кілька секунд
        return out

    async with httpx.AsyncClient(timeout=30) as client:
        results = await asyncio.gather(*[
            query_all_pages(client, t.format(city=city)) for t in _PLACES_QUERIES
        ])
    merged: dict[str, dict] = {}
    for part in results:
        for key_id, value in part.items():
            merged.setdefault(key_id, value)
    return list(merged.values())


# ---------------------------------------------------------------- OSM
async def find_agencies_osm(city: str) -> list[dict]:
    query = (
        '[out:json][timeout:25];'
        f'area["name"="{city}"]["boundary"="administrative"]->.a;'
        'nwr["office"="estate_agent"](area.a);'
        'out center tags 400;'
    )
    try:
        async with httpx.AsyncClient(timeout=40, headers=_UA) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            out: dict[str, dict] = {}
            for el in (resp.json().get("elements") or []):
                tags = el.get("tags") or {}
                name = tags.get("name") or ""
                website = tags.get("website") or tags.get("contact:website") or ""
                phone = tags.get("phone") or tags.get("contact:phone") or ""
                kid = _domain(website) or name
                if not kid or kid in out:
                    continue
                out[kid] = {
                    "name": name, "website": website, "phone": phone,
                    "address": "", "source": "osm",
                }
            return list(out.values())
    except Exception as error:
        print(f"dubl catalog osm error: {type(error).__name__}: {error!r}", flush=True)
        return []


async def find_agencies_osm_country() -> list[dict]:
    """РАЗОВИЙ сид: усі office=estate_agent по Україні (з addr:city як місто)."""
    query = (
        '[out:json][timeout:240];'
        'area["name"="Україна"]["admin_level"="2"]->.a;'
        'nwr["office"="estate_agent"](area.a);'
        'out center tags;'
    )
    try:
        async with httpx.AsyncClient(timeout=260, headers=_UA) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            out: dict[str, dict] = {}
            for el in (resp.json().get("elements") or []):
                tags = el.get("tags") or {}
                website = tags.get("website") or tags.get("contact:website") or ""
                dom = _domain(website)
                if not dom or dom in out:
                    continue
                out[dom] = {
                    "name": tags.get("name") or "",
                    "website": website,
                    "phone": tags.get("phone") or tags.get("contact:phone") or "",
                    "city": tags.get("addr:city") or "",
                    "source": "osm",
                }
            return list(out.values())
    except Exception as error:
        print(f"dubl catalog osm-country error: {type(error).__name__}: {error!r}", flush=True)
        return []


# ---------------------------------------------------------------- merge
async def find_agencies(city: str) -> list[dict]:
    """Об'єднаний список агентств міста (Places + OSM + Serper), дедуплікований."""
    if not city:
        return []
    places, osm, serper = await asyncio.gather(
        find_agencies_places(city),
        find_agencies_osm(city),
        find_agencies_serper(city),
    )
    merged: dict[str, dict] = {}
    # Places/OSM першими — у них надійніше є сайт і телефон; Serper добирає обсяг.
    for item in places + osm + serper:
        kid = _domain(item.get("website")) or (item.get("name") or "")
        if not kid or kid in merged:
            continue
        merged[kid] = item
    # Повертаємо ЛИШЕ агентства з реальним сайтом — їх можна сканувати.
    # (OSM-записи без тега website відкидаємо: самої назви для пошуку замало.)
    return [item for item in merged.values() if item.get("website")]
