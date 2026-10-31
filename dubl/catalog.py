"""M2: обнаружение агентств нерухомості по місту.

Два безкоштовні/наявні джерела:
  • Google Places API (New) — пошук агентств по місту з сайтом і телефоном;
  • OpenStreetMap Overpass — office=estate_agent у межах міста.

Повертаємо ЛИШЕ посилання/контакти (name, website, phone). Нічого тут не
зберігаємо на диск — каталог-сховище підключимо окремим кроком.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

from dubl.config import settings

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_UA = {"User-Agent": "dubl.top/0.1 (real-estate duplicate finder)"}


def _domain(url: str | None) -> str:
    """Нормалізований домен для дедуплікації (без www)."""
    try:
        net = urlparse(url or "").netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


async def find_agencies_places(city: str) -> list[dict]:
    """Агентства в місті через Google Places API (New)."""
    key = settings.google_places_api_key
    if not key:
        return []
    queries = [
        f"агентство нерухомості {city}",
        f"ріелтор {city}",
        f"нерухомість продаж оренда {city}",
    ]
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": (
            "places.displayName,places.websiteUri,"
            "places.nationalPhoneNumber,places.formattedAddress,places.id"
        ),
    }
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=20) as client:
        for q in queries:
            try:
                resp = await client.post(PLACES_URL, headers=headers, json={
                    "textQuery": q, "languageCode": "uk", "regionCode": "UA", "pageSize": 20,
                })
                resp.raise_for_status()
                for place in (resp.json().get("places") or []):
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
            except Exception as error:
                print(f"dubl catalog places error ({q}): {error}", flush=True)
    return list(out.values())


async def find_agencies_osm(city: str) -> list[dict]:
    """Агентства в місті через OpenStreetMap Overpass (office=estate_agent)."""
    query = (
        '[out:json][timeout:25];'
        f'area["name"="{city}"]->.a;'
        'nwr["office"="estate_agent"](area.a);'
        'out center tags 80;'
    )
    try:
        async with httpx.AsyncClient(timeout=35, headers=_UA) as client:
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
        print(f"dubl catalog osm error: {error}", flush=True)
        return []


# Великі портали й не-агентські сайти — не вважаємо їх сайтами агентств
# (їх обходимо окремо на етапі зрізу цін).
_NON_AGENCY_DOMAINS = {
    "olx.ua", "dom.ria.com", "ria.com", "lun.ua", "rieltor.ua", "m2bomber.com",
    "flatfy.ua", "address.ua", "prom.ua", "ua.all.biz", "blizko.ua",
    "facebook.com", "instagram.com", "youtube.com", "t.me", "tiktok.com",
    "wikipedia.org", "google.com", "maps.google.com", "2gis.ua",
}


async def serper_search(query: str, num: int = 20) -> list[dict]:
    """Веб-пошук Google через Serper.dev. Повертає organic-результати."""
    key = settings.serper_api_key
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": query, "gl": "ua", "hl": "uk", "num": num},
            )
            resp.raise_for_status()
            return resp.json().get("organic") or []
    except Exception as error:
        print(f"dubl catalog serper error ({query}): {error}", flush=True)
        return []


async def find_agencies_serper(city: str) -> list[dict]:
    """Сайти агентств з веб-пошуку Google (Serper), без великих порталів."""
    results = await serper_search(f"агентства нерухомості {city}")
    out: dict[str, dict] = {}
    for item in results:
        dom = _domain(item.get("link"))
        if not dom or dom in _NON_AGENCY_DOMAINS or dom in out:
            continue
        out[dom] = {
            "name": item.get("title") or dom,
            "website": "https://" + dom,
            "phone": "",
            "address": "",
            "source": "serper",
        }
    return list(out.values())


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
    # Places/OSM першими — у них надійніше є сайт і телефон; Serper добирає охоплення.
    for item in places + osm + serper:
        kid = _domain(item.get("website")) or (item.get("name") or "")
        if not kid or kid in merged:
            continue
        merged[kid] = item
    return list(merged.values())
