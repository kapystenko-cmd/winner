"""Парсинг OLX через ScraperAPI або ZenRows"""
import re
import os
import io
import json
import random
import asyncio
import urllib.parse
import html as html_lib
from pathlib import Path
from typing import Optional
import httpx
from PIL import Image as PILImage
from app.core.config import settings


# A real-estate asking price of a few hryvnias is always an extraction error
# (usually a room number, area or a monthly-payment fragment from page HTML).
# Do not try to guess a missing number of thousands: reject such a card and
# continue with other market listings instead.
MIN_MARKET_PRICE_UAH = 5_000

# Confirmed real OLX category paths (example verified by the product owner:
# olx.ua/uk/nedvizhimost/doma/prodazha-domov/kharkov/ = houses for sale in
# Kharkiv). OLX keeps these path segments in Russian transliteration even
# under the /uk/ locale prefix -- there is no Ukrainian-language slug variant.
# Apartments are deliberately left out of this map and keep using the older
# site-wide search below: that search was separately confirmed working well
# for apartments, and changing it is out of scope for this fix (houses/land
# were the ones reported as returning irrelevant or empty results).
_OLX_CATEGORY_PATHS = {
    "house": ("doma", "prodazha-domov"),
    "land": ("zemlya", "prodazha-zemli"),
}

# Only a handful of major cities' OLX category-page slugs are confirmed
# (Russian transliteration, e.g. "kiev", not "kyiv"/"київ"). Guessing the rest
# risks a dead category page for a city we have not verified. Any city NOT in
# this map simply skips the city path segment below and relies on the
# free-text "q-" filter inside the category instead (confirmed to work with
# or without a preceding city segment), so no city search is ever blocked by
# a missing slug -- it is only less precisely scoped.
_OLX_CITY_CATEGORY_SLUGS = {
    "київ": "kiev", "киев": "kiev",
    "харків": "kharkov", "харьков": "kharkov",
    "одеса": "odessa", "одесса": "odessa",
    "львів": "lvov", "львов": "lvov",
    "дніпро": "dnepr", "дніпропетровськ": "dnepr", "днепр": "dnepr", "днепропетровск": "dnepr",
    "запоріжжя": "zaporozhe", "запорожье": "zaporozhe",
}



def _money_number(raw_value: str) -> float:
    """Parse OLX money written as ``8 500``, ``8,500``, ``8500`` or ``246 670.17``.

    The function treats punctuation followed by exactly three digits as a
    thousands separator.  A previous version did that unconditionally, which
    also matched the LAST separator in a value that ends with two-digit
    cents (e.g. ``246 670.17``): the ``.17`` was misread as more integer
    digits instead of being dropped, inflating a real price of 246 670 UAH
    into 24 667 017 UAH — a ~100x error that then failed every e-certificate
    price-corridor check even for a genuinely matching listing. The trailing
    cents suffix is stripped first, before thousands-separator handling.
    """
    compact = html_lib.unescape(str(raw_value or "")).replace("\u00a0", " ").strip()
    compact = re.sub(r"[,.]\d{2}(?!\d)$", "", compact)
    compact = re.sub(r"(?<=\d)[,\.](?=\d{3}(?:\D|$))", "", compact)
    compact = re.sub(r"\s+", "", compact)
    digits = re.sub(r"\D", "", compact)
    return float(digits) if digits else 0.0


def _money_from_text(text: str, usd_rate: float = 1.0) -> float:
    """Return the most plausible explicitly-currency-labelled price in text."""
    readable = html_lib.unescape(str(text or ""))
    # The first price in an OLX page title is the listing asking price.  The
    # page body can also include unrelated subscription and advertising sums,
    # therefore callers pass a title before passing broad page HTML.
    #
    # OLX titles write a dollar-denominated price BOTH ways — "8 500 $" and
    # "$ 8 500" / "$8 500" — and some ads use "у.о." (умовні одиниці) instead
    # of "$" for the same thing. A previous version only matched
    # number-then-currency, so any "$"-prefixed or "у.о."-suffixed USD price
    # was silently invisible to the parser (amount 0, listing dropped) even
    # though it was a perfectly real, matching asking price.
    pattern = re.compile(
        r"(?<!\d)(?P<amt1>\d(?:[\d\s\u00a0,.]*\d)?)\s*(?P<cur1>грн|uah|₴|usd|у\.?о\.?|\$)(?![\w])"
        r"|"
        r"(?P<cur2>грн|uah|₴|usd|у\.?о\.?|\$)\s*(?P<amt2>\d(?:[\d\s\u00a0,.]*\d)?)(?!\d)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(readable):
        amount = _money_number(match.group("amt1") or match.group("amt2"))
        currency = (match.group("cur1") or match.group("cur2")).casefold()
        if currency in {"usd", "$"} or currency.startswith("у"):
            amount *= usd_rate
        if amount >= MIN_MARKET_PRICE_UAH:
            return round(amount)
    return 0.0


def _extract_olx_price(html: str, title: str, usd_rate: float) -> float:
    """Extract a listing price without mistaking an area/room value for price."""
    # Title normally has the canonical form "…: 8 500 $ - Продаж …".
    price = _money_from_text(title, usd_rate)
    if price:
        return price

    # Then examine semantic metadata / JSON snippets, before the entire page.
    semantic_chunks = re.findall(
        r"(?:price|priceLabel|priceValue|amount)[^>{]{0,120}[>:=]\s*[^<>{]{0,180}",
        html,
        re.IGNORECASE,
    )
    for chunk in semantic_chunks:
        price = _money_from_text(chunk, usd_rate)
        if price:
            return price

    # Last fallback for markup variants which do not expose a title.  It is
    # still currency-labelled and guarded by MIN_MARKET_PRICE_UAH.
    return _money_from_text(html, usd_rate)


async def get_usd_rate():
    """Курс USD/UAH з НБУ"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
            )
            resp.raise_for_status()
            data = resp.json()
            if data and len(data) > 0:
                rate = float(data[0]["rate"])
                print("NBU USD rate: " + str(rate))
                return rate
    except Exception as e:
        print("NBU rate error: " + str(e))
    return 41.5


def _normalise_region_word(region):
    """Return an oblast name in one canonical "X область" form.

    OCR returns the region in whichever form the document used: "Чернігівська",
    "Чернігівська обл.", "ЧЕРНІГІВСЬКА ОБЛАСТЬ". A previous version only checked
    for the substring "област", so "Чернігівська обл." passed the check and got
    " область" appended anyway, producing the query
    "куплю будинок Чернігівська обл. область" -- seen in a real search URL.
    Strip any existing suffix first, then add exactly one.
    """
    if not region:
        return None
    text = re.sub(r"\s+", " ", str(region)).strip()
    text = re.sub(r"\s*(область|обл\.?)\s*$", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return None
    return text + " область"


def _normalize_city(city_name):
    city = city_name.lower().strip()
    for prefix in ["м.", "м ", "місто ", "г.", "г ", "город ",
                    "с.", "с ", "село ", "смт.", "смт ", "селище "]:
        if city.startswith(prefix):
            city = city[len(prefix):].strip()
    return city


def _olx_city_slug(city):
    """Known OLX location slugs; category URLs are more reliable than q-search."""
    cleaned = city.lower().replace("'", "").replace("’", "").replace(" ", "")
    aliases = {
        "словянськ": "slavyansk",
        "славянск": "slavyansk",
        "київ": "kiev",
        "киев": "kiev",
    }
    return aliases.get(cleaned)


def _build_olx_search_url(city, property_type="apartment", page=1, rooms=None, district=None, room_word="ком", variant="rooms", region=None):
    # City goes into the OLX q-search as-is. Previously the two rstrip("у")/
    # rstrip("і") calls stripped dative/locative endings from every city name,
    # turning "Полтава" into "полтав" and calibrating the code so that only
    # a manual hack ("if 'слов' in city_clean") could salvage one city. OLX
    # search understands nominative names in Ukrainian and Russian directly;
    # each name now reaches it unchanged, so Полтава/Харків/Одеса/etc stop
    # being crippled, and Словянськ works via its own nominative form without
    # a special case.
    city_clean = _normalize_city(city).strip().replace("'", "").replace("\u2019", "")
    labels = {"apartment": "квартиру", "house": "будинок", "land": "ділянку", "commercial": "приміщення"}
    label = labels.get(property_type, "квартиру")
    # Site-wide /uk/list/q-.../ search -- kept for apartments only (confirmed
    # working well there). For houses/land, a real run showed this matching
    # too broadly: a thin house market made OLX backfill the page with
    # unrelated ads, and the plain "продаж будинку {місто}" text alone did
    # not keep results on-topic. Those two types are now scoped to their real
    # OLX category first (see _OLX_CATEGORY_PATHS above); the free-text query
    # built below is unchanged and applies inside that category via "q-".
    # For houses/land in non-major cities: add region (область) to the query
    # so OLX scopes results geographically instead of backfilling with garbage.
    # "куплю будинок борзна" → "куплю будинок борзна чернігівська область"
    # Apartments and major cities are unchanged.
    is_major_city = city_clean.casefold() in _OLX_CITY_CATEGORY_SLUGS
    add_region = (
        region
        and property_type in ("house", "land")
        and not is_major_city
    )
    # For region, always append "область" so OLX scopes geographically:
    # "Чернігівська" → "Чернігівська область"
    region_word = _normalise_region_word(region) if add_region else None

    if add_region:
        # For houses/land in non-major cities: use a SIMPLE query matching
        # the format that OLX handles best (confirmed by real tests):
        #   "куплю будинок чернігівська область"
        # Do NOT include room count or city name — OLX backfills with
        # garbage (iPhones, calculators) when the query is too specific
        # for a thin market. The downstream area/room/price filters
        # will narrow from the regional pool.
        words = [w for w in ["куплю", label, region_word] if w]
    elif variant == "legacy":
        prop_names = {"apartment": "квартири", "house": "будинку", "land": "ділянки", "commercial": "комерційна"}
        words = [w for w in ["продаж", prop_names.get(property_type, "квартири"), city_clean, region_word, district if district else None] if w]
    elif rooms:
        words = [w for w in [
            "куплю", str(int(rooms)), room_word, label, city_clean,
            region_word,
            district if district else None,
        ] if w]
    else:
        words = [w for w in ["куплю", label, city_clean, region_word, district if district else None] if w]
    query_text = "-".join(str(word).strip().replace(" ", "-") for word in words)
    query = urllib.parse.quote(query_text, safe="-")
    category_path = _OLX_CATEGORY_PATHS.get(property_type)
    if category_path:
        category_slug, sale_slug = category_path
        base_url = "https://www.olx.ua/uk/nedvizhimost/" + category_slug + "/" + sale_slug + "/"
        city_slug = _OLX_CITY_CATEGORY_SLUGS.get(city_clean.casefold())
        if city_slug:
            base_url += city_slug + "/"
        url = base_url + "q-" + query + "/"
    else:
        url = "https://www.olx.ua/uk/list/q-" + query + "/"
    if page > 1:
        url += "?page=" + str(page)
    return url


async def _fetch_page(url, timeout_seconds=20):
    """Fetch one search or listing page through the configured provider.

    A bounded timeout is important here: 20 slow details with a five-slot
    pool must finish or fail within the two-minute report limit, not leave an
    evaluator waiting behind one blocked marketplace response.
    """
    is_olx = "olx.ua" in url.casefold()

    def has_cards(content):
        return bool(re.search(r'/d/(?:uk/|ru/)?obyavlenie/', content or "", re.IGNORECASE))

    # Try ScraperAPI first
    scraper_key = getattr(settings, "scraper_api_key", "") or os.environ.get("SCRAPER_API_KEY", "")
    if scraper_key and getattr(settings, "scraperapi_enabled", True):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=8)) as client:
                resp = await client.get("https://api.scraperapi.com", params={
                    "api_key": scraper_key,
                    "url": url,
                })
                resp.raise_for_status()
                content = resp.text
                # A previous version returned this response unconditionally.
                # ScraperAPI's default (non-rendered) fetch of an OLX search
                # page can come back as a valid 200 response that is still
                # just the JS-app shell, with zero listing cards in it. That
                # silently produced "0 found" on every query, indistinguishable
                # from a genuinely empty market. Verify card presence and, if
                # missing, pay for one JS-rendered retry before giving up on
                # ScraperAPI entirely.
                if not is_olx or has_cards(content):
                    return content
                print("ScraperAPI basic HTML did not contain OLX cards; retrying with render=true")
                rendered = await client.get("https://api.scraperapi.com", params={
                    "api_key": scraper_key,
                    "url": url,
                    "render": "true",
                })
                rendered.raise_for_status()
                if has_cards(rendered.text):
                    return rendered.text
                print("ScraperAPI render=true still had no OLX cards; falling back to ZenRows")
        except Exception as e:
            print("ScraperAPI error: " + str(e))

    # Fallback to ZenRows.  HTML extraction must start in the inexpensive
    # non-JavaScript mode: OLX often returns the listing URLs and structured
    # detail text in the initial response.  JavaScript rendering is requested
    # only when that response does not contain an OLX listing card at all.
    # Screenshots use their own JavaScript-enabled branch below.
    if settings.zenrows_api_key:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=8)) as client:
                basic = await client.get("https://api.zenrows.com/v1/", params={
                    "apikey": settings.zenrows_api_key,
                    "url": url,
                })
                basic.raise_for_status()
                content = basic.text
                # Both OLX category pages and card pages contain this route
                # when the real server HTML was delivered.  A challenge page
                # does not, therefore it is safe to spend one rendered retry
                # only in that exceptional case.
                if not is_olx or has_cards(content):
                    print("ZenRows HTML mode: basic")
                    return content
                print("ZenRows basic HTML did not contain OLX cards; retrying with JavaScript")
                rendered = await client.get("https://api.zenrows.com/v1/", params={
                    "apikey": settings.zenrows_api_key,
                    "url": url,
                    "js_render": "true",
                    "wait": 2500,
                })
                rendered.raise_for_status()
                print("ZenRows HTML mode: JavaScript fallback")
                return rendered.text
        except Exception as e:
            print("ZenRows error: " + str(e))

    if not scraper_key and not settings.zenrows_api_key:
        print("No scraper API configured (SCRAPER_API_KEY and ZENROWS_API_KEY both missing)")
    else:
        print("Fetch failed after all configured providers/retries: " + url)
    return None


async def _take_screenshot(url, save_path):
    """Create evidence from the selected OLX card, never from an author page.

    Pipeline (one simple path, no Gemini, no fallbacks that always fail):
      ZenRows screenshot endpoint, JPEG, zoom 0.5, 900x1600 viewport ->
      byte crop in report_generator.
    OLX blocks datacenter IPs, so a local Playwright visit is refused; the
    old "server browser via ZenRows proxy" / "render ZenRows HTML" fallbacks
    always failed with "title/price was not ready" and only burned 15-20s
    each per analog. They are removed.
    """
    if not settings.zenrows_api_key:
        print("OLX screenshot skipped: no ZENROWS_API_KEY configured")
        return False

    primary = Path(save_path)
    # Chrome at 50% zoom inside a 900x1600 viewport. Zoom lets the OLX
    # listing (gallery + title + price + first specs) fit into one frame
    # instead of a shallow desktop strip. hide_css also removes cookie
    # banners that would otherwise cover the price block.
    # Ad + recommendation selectors confirmed by live DOM inspection across 7
    # different OLX listings (house/townhouse/duplex/part-house/2-storey): all
    # ad slots carry id^="baxter-" AND data-testid="qa-advert-slot" (the top
    # banner that pushed the опис out of frame is baxter-top; others:
    # under-price, parameters, middle, right-column); the grey "Схожі
    # оголошення" skeleton grid below the card is data-testid="ad-recommendations"
    # and the author's-listings sliders are data-testid="adlist-slider". Hiding
    # these at capture time removes the FILLED ad banners that the pixel crop
    # cannot distinguish from real content, so the capture is the clean card
    # (photo+title+price+specs+опис+seller+map) every time. These testids are
    # stable (not the churning css-* classes), so this is robust to redesigns.
    # Capture zoom is tunable live via OLX_SCREENSHOT_ZOOM. zoom:0.5 shrinks the
    # whole page to 50%, which is what lets the two-column card fit a portrait
    # frame — but it also HALVES the pixel density of the listing text, which is
    # the main cause of soft/unreadable опис text in the report. Since we now
    # capture fullpage and crop to the card anyway (fit no longer depends on
    # zoom), a larger value keeps more text resolution: 0.5 = smallest/safest,
    # 0.67 ~ +33% text pixels, 1.0 = full desktop resolution (sharpest, tallest
    # frame). Default stays 0.5 so nothing changes until tested; raise it on the
    # server and compare sharpness without a code change.
    try:
        _zoom = float(os.environ.get("OLX_SCREENSHOT_ZOOM", "0.5") or 0.5)
    except ValueError:
        _zoom = 1.0
    _zoom = min(1.0, max(0.3, _zoom))
    # Full-page capture at full resolution (no shrink), then the server crops.
    # Architecture (per user): ZenRows takes ONE full-page screenshot of the
    # whole listing at natural desktop resolution; before the shot it dismisses
    # the cookie bar, hides the ad/recommendation blocks, and — critically for
    # full-page — neutralises every position:fixed / :sticky element. Fixed
    # elements (the top nav, the cookie overlay, the sticky right-column) are
    # otherwise re-painted onto EVERY stitched segment of a full-page capture,
    # which is exactly the repeating "полоса" seen before. Setting them to
    # position:static makes each appear once, in normal flow. smart_crop_listing
    # then trims rails + the empty top band and caps the aspect to the template.
    # zoom defaults to 1.0 now (full text resolution); OLX_SCREENSHOT_ZOOM still
    # overrides it. Selectors (cookies/baxter/recommendations) confirmed by live
    # DOM inspection across 7 listings; cookie dismiss btn = dismiss-cookies-banner.
    prep_js = (
        "try{var b=document.querySelector('[data-testid=\"dismiss-cookies-banner\"]');if(b)b.click();}catch(e){}"
        "try{var pats=['прийня','погодж','приймаю','дозволит','зрозум'];"
        "var cands=[].slice.call(document.querySelectorAll('button,[role=\"button\"],a'));"
        "for(var i=0;i<cands.length;i++){var t=(cands[i].textContent||'').trim().toLowerCase();"
        "if(t.length<40&&pats.some(function(p){return t.indexOf(p)>-1;})){cands[i].click();break;}}}catch(e){}"
        "var css='body{zoom:" + str(_zoom) + " !important;}"
        "[data-testid=\"cookies-bar\"],[data-cy=\"cookies-bar\"],[data-testid=\"cookies-overlay__container\"],[data-testid*=\"cookies\"],"
        "#onetrust-banner-sdk,.cookie-banner,[class*=\"cookie\"],"
        "[id^=\"baxter-\"],[data-testid=\"qa-advert-slot\"],[data-testid=\"ad-slot\"],"
        "[data-testid=\"ad-recommendations\"],[data-testid=\"adlist-slider\"],[class*=\"skeleton\"]"
        "{display:none !important;visibility:hidden !important;height:0 !important;}';"
        "var s=document.createElement('style');s.innerHTML=css;document.head.appendChild(s);"
        "try{var all=document.querySelectorAll('body *');for(var j=0;j<all.length;j++){"
        "var p=getComputedStyle(all[j]).position;if(p==='fixed'||p==='sticky'){"
        "all[j].style.setProperty('position','static','important');}}}catch(e){}"
    )
    # Optional override: OLX_SCREENSHOT_SELECTOR env for element-only capture via
    # ZenRows screenshot_selector (mutually exclusive with fullpage — REQS004).
    selector = (os.environ.get("OLX_SCREENSHOT_SELECTOR", "") or "").strip()
    params = {
        "apikey": settings.zenrows_api_key,
        "url": url,
        "screenshot_fullpage": "true",
        "screenshot_format": "jpeg",
        "screenshot_quality": 92,
        "wait": 1000,
        "js_render": "true",
        "js_instructions": json.dumps([
            {"wait": 1500}, {"evaluate": prep_js}, {"wait": 1800}
        ]),
    }
    if selector:
        # Element-only server-side crop override. fullpage and selector cannot
        # both be set (ZenRows 400 REQS004), so drop fullpage when a selector
        # is given; the prep js_instructions still run.
        params["screenshot_selector"] = selector
        params.pop("screenshot_fullpage", None)
        print(f"OLX screenshot: using selector '{selector}'")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get("https://api.zenrows.com/v1/", params=params)
            response.raise_for_status()
        content_type = str(response.headers.get("content-type", "")).casefold()
        is_image = (
            len(response.content) > 1024
            and ("image/" in content_type
                 or response.content.startswith(b"\x89PNG")
                 or response.content.startswith(b"\xff\xd8"))
        )
        if not is_image:
            print("Screenshot ZR error: ZenRows did not return an image for " + url)
            return False
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_bytes(response.content)
        try:
            with PILImage.open(io.BytesIO(response.content)) as shot:
                w, h = shot.size
                print(f"OLX raw ZenRows capture: {w}x{h}: {url}")
                if w > h:
                    print(f"OLX WARNING: landscape capture ({w}x{h}) — zoom:0.5 CSS likely did not apply before screenshot")
        except Exception:
            pass
        # Percentage crop is applied once, centrally, in report_generator.py.
        print("OLX screenshot created through ZenRows: " + url)
        return True
    except httpx.HTTPStatusError as e:
        body_snippet = ""
        try:
            body_snippet = e.response.text[:300]
        except Exception:
            pass
        print(f"Screenshot ZR error: HTTP {e.response.status_code} for url={e.request.url} body={body_snippet!r}")
    except Exception as e:
        print(f"Screenshot ZR error: {type(e).__name__}: {e!r}")
    return False


def _listing_preview(url, page_html, position):
    """Read the inexpensive information already present in an OLX result list.

    This is only a prioritisation hint, never final evidence.  The selected
    cards are still opened and parsed by ``get_listing_details`` before they
    can become an analogue in a report.
    """
    # ``position`` is already the exact match start from the caller's regex
    # scan (see _parse_search_page). Re-scanning the *entire* page from the
    # start for a reconstructed needle — for every one of dozens of matches
    # on a busy page — was observed to stall the whole worker process on this
    # server (systemd auto-restart, no Python traceback: the request simply
    # never came back). Use the known position directly; only fall back to a
    # full-page search if no position was supplied.
    start = position if position is not None and position >= 0 else -1
    if start < 0:
        needle = 'href="' + urllib.parse.urlsplit(url).path + '"'
        found_at = page_html.find(needle)
        start = found_at if found_at >= 0 else 0
    # OLX result-card markup carries the title, price and often room/area in
    # the same compact block.  Reading this block avoids spending a proxy
    # request on clearly unsuitable advertisements.
    fragment = page_html[max(0, start - 350): start + 2800]
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))
    text = re.sub(r"\s+", " ", text).strip()
    title_match = re.search(r'<a[^>]+href="' + re.escape(urllib.parse.urlsplit(url).path) + r'"[^>]*>(.*?)</a>', fragment, re.I | re.S)
    title = html_lib.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1))).strip() if title_match else ""
    title = re.sub(r"\s+", " ", title)[:300] or text[:300]

    price = _money_from_text(text)
    area = None
    area_match = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м)", text, re.I)
    if area_match:
        try:
            area = float(area_match.group(1).replace(",", "."))
        except ValueError:
            pass
    rooms = None
    room_match = re.search(r"(?:^|[\s,;:/_-])(\d+)\s*(?:кімн|кімнат|комн|комнат|room)", text + " " + url, re.I)
    if room_match:
        try:
            rooms = int(room_match.group(1))
        except ValueError:
            pass
    return {"title": title, "price_uah": price, "area_sqm": area, "rooms": rooms, "_preview_text": text[:900]}


def _parse_search_page(html, max_results=30):
    """Collect card URLs from a search-result page defensively.

    OLX's listing-page markup changes noticeably more often than its card-page
    markup. A single anchor pattern silently returning zero listings used to
    end the whole OLX search after one page (search_olx stops early on a thin
    page). Several independent, narrower patterns are combined here so that
    one markup change does not zero out the entire source.
    """
    listings = []
    seen = set()
    # 1) Classic relative anchor, locale-agnostic (catches /d/uk/, /d/ru/, or
    #    a missing locale segment; OLX has used all three historically).
    # 2) Absolute anchor variant, in case links are rendered as full URLs.
    # 3) A bare path appearing anywhere (href attribute, data-href, or inside
    #    an embedded JSON state blob), matched without requiring the href=
    #    wrapper at all. This is intentionally the most permissive pattern and
    #    is only used to fill in URLs the stricter patterns missed.
    patterns = [
        re.compile(r'href="(/d/(?:uk/|ru/)?obyavlenie/[^"?#]+)', re.IGNORECASE),
        re.compile(r'href="(https?://www\.olx\.ua/d/(?:uk/|ru/)?obyavlenie/[^"?#]+)', re.IGNORECASE),
        re.compile(r'"(/d/(?:uk/|ru/)?obyavlenie/[^"?#\\]+)"', re.IGNORECASE),
    ]
    matched_by_pattern = []
    # Cap how many raw matches get a preview built. Only ~12 candidates are
    # ever opened for detail; building previews for every one of a few hundred
    # raw matches (as could happen on a large city with two language variants
    # across two pages) burns CPU/time for no benefit and, on a small VPS, was
    # observed to stall the whole worker process (systemd auto-restart, no
    # Python traceback — the request simply never came back).
    PREVIEW_BUDGET = 100
    for index, pattern in enumerate(patterns):
        count = 0
        for match in pattern.finditer(html):
            count += 1
            if len(listings) >= PREVIEW_BUDGET:
                continue
            raw = match.group(1)
            path = raw if raw.startswith("/") else "/" + raw.split("olx.ua", 1)[-1].lstrip("/")
            if path in seen:
                continue
            seen.add(path)
            full_url = "https://www.olx.ua" + path
            if not full_url.endswith(".html"):
                full_url += ".html"
            # match.start() is already the exact position of this match; no
            # need to re-scan the whole page again to find it.
            preview = _listing_preview(full_url, html, match.start())
            listings.append({"source": "olx", "url": full_url, **preview})
        matched_by_pattern.append(count)
    if not listings:
        # Nothing matched at all: log enough of the raw response to diagnose
        # a markup change or a block/challenge page on the next real run,
        # instead of silently returning an empty page.
        looks_like_challenge = any(
            marker in html.casefold() for marker in ("captcha", "attention required", "just a moment", "cf-browser-verification")
        )
        print(
            f"OLX list parse found 0 cards: html_length={len(html)}, "
            f"looks_like_challenge={looks_like_challenge}, sample={html[:400]!r}",
            flush=True,
        )
    else:
        print(f"OLX list parse matched per pattern: {matched_by_pattern}", flush=True)
    return listings[:max_results]


def _prioritize_listings(listings, rooms=None, area_sqm=None, district=None, property_type="apartment", city=None):
    """Rank list-page cards before paid detail-page requests.

    Known mismatch on rooms or area is excluded early.  Missing list metadata
    is retained with a low score, since OLX often exposes it only on the card.
    """
    district_text = str(district or "").casefold().strip()
    # OLX's own search silently broadens beyond the requested city when local
    # matches are thin (the same "extended_search" behaviour seen expanding
    # room count also expands geography) — a real run for Slov'yansk returned
    # Kyiv apartments (Дragomanova St, ЖК Щасливий, Позняки) at 3.6-4.9M грн,
    # ~10-20x the local market, which would silently distort statistics if
    # the price corridor happened to be wide enough to admit them. A listing
    # naming a DIFFERENT major city is rejected; one that names the target
    # city, or names no city at all, is kept (many cards omit the city when
    # it's implied by the search itself).
    other_major_cities_latin = (
        "kiev", "kyiv", "kyev", "m-kiv", "kharkiv", "kharkov", "odesa", "odessa", "dnipro",
        "lviv", "zaporizh", "vinnytsia", "vinnica", "poltava", "chernihiv",
        "cherkasy", "sumy", "zhytomyr", "rivne", "khmelnytsk", "chernivtsi",
        "ternopil", "ivano-frankivsk", "lutsk", "uzhhorod", "uzhgorod",
        "kropyvnytskyi", "mykolaiv", "nikolaev", "kherson", "mariupol",
    )
    other_major_cities_cyrillic = (
        "київ", "киев", "харків", "харьков", "одеса", "одесса", "дніпро",
        "львів", "запоріж", "вінниц", "полтав", "чернігів", "черкас",
        "суми", "житомир", "рівне", "хмельницьк", "чернівц", "тернопіл",
        "івано-франківськ", "луцьк", "ужгород", "кропивницьк", "миколаїв",
        "херсон", "маріупол",
    )
    target_city = str(city or "").casefold().strip()
    target_city_latin = _normalize_city(target_city).replace("'", "").replace("\u2019", "")

    def matches_city(item):
        if not target_city:
            return True
        slug = str(item.get("url") or "").casefold()
        haystack = (str(item.get("title") or "") + " " + str(item.get("_preview_text") or "")).casefold()
        if target_city in haystack or (target_city_latin and target_city_latin in slug):
            return True
        blocked_cities_latin = tuple(c for c in other_major_cities_latin if c not in target_city_latin and target_city_latin not in c)
        blocked_cities_cyrillic = tuple(c for c in other_major_cities_cyrillic if c not in target_city and target_city not in c)
        if any(marker in slug for marker in blocked_cities_latin) or \
                any(marker in haystack for marker in blocked_cities_cyrillic):
            return False
        return True

    # A second, independent safety net against off-category cards slipping
    # in. OLX's own free-text search under "nedvizhimost/q-..." was observed
    # (real test, screenshot) to search the WHOLE real-estate parent category
    # regardless of which subcategory path precedes "q-", and — when exact
    # matches are thin — even backfills with completely unrelated categories
    # (a school backpack, a POS terminal and kittens were all seen in a real
    # run's detail-scan queue). An earlier version of this filter *required*
    # an explicit apartment/room word in the slug or preview text; that
    # rejected 65 of 69 real candidates on a real run because plenty of
    # genuine listings never spell out "квартира"/"кімната" in their title
    # (e.g. "Терміново! 45м², Соцмісто"). Requiring positive real-estate
    # vocabulary is too strict; excluding explicit signals of the WRONG
    # category is both safer for recall and enough to keep out tyres,
    # clothing, electronics, animals and the wrong real-estate subtype.
    non_realty_exclude_latin = (
        "shin-", "-shin-", "kolesa", "avtozapchast", "zapchast",
        "koshenya", "kotenok", "kotyata", "shhenok", "shenyat", "sobak",
        "kofta", "plate-", "vzuttya", "obuv", "sumka", "ryukzak", "odezhda", "odyag",
        "telefon", "smartfon", "noutbuk", "kompyuter", "termnal", "termina", "planshet",
        "holodilnik", "pralna", "-mebli", "mebli-", "divan-", "shafa",
        "instrument-", "budmaterial", "stroymaterial", "igrash", "kolyask",
        "velosiped", "motocikl", "skuter",
    )
    non_realty_exclude_cyrillic = (
        "шин", "колеса", "запчаст", "кошеня", "котен", "кошенят", "щеня", "щенят", "собак",
        "кофта", "плаття", "взуття", "сумка", "рюкзак", "одяг", "одежда",
        "телефон", "смартфон", "ноутбук", "комп'ютер", "планшет", "термінал", "терминал",
        "холодильник", "пральна", "меблі", "диван", "шафа",
        "інструмент", "будматеріал", "стройматериал", "іграш", "коляск",
        "велосипед", "мотоцикл", "скутер",
    )
    # The app values a property for SALE, never for rent. Rental ads for a
    # 2-room flat in Slov'yansk run ~5 000-10 000 грн/month — a real, valid
    # number that is nonetheless nowhere near a sale price. A real run showed
    # these dominating the "price too low" rejections (a rental card is
    # structurally incapable of landing in a sale price corridor), burning
    # detail-scan budget on cards that could never be a sale comparable.
    rental_exclude_latin = (
        "sdam-", "sdau-", "sdayu-", "sdat-", "sdaetsya", "sdayetsya",
        "zdam-", "zdau-", "zdayu-", "zdat-", "zdaetsya", "zdayetsya",
        # "arenda"/"аренда" (Russian) transliterates with an "a"; the
        # Ukrainian "оренда" transliterates with an "o" — a real run showed
        # "orenda-2-kmn-kvartiri..." slipping past the list-level filter and
        # only getting caught later by the price check (an 18 000 грн/month
        # rent, correctly rejected — but after wasting a detail-scan slot).
        "arenda-", "-arendu", "arendu-", "orenda-", "-orendu", "orendu-",
        "posutochno", "pochasovo", "dobovo-",
    )
    rental_exclude_cyrillic = (
        "здам", "здаю", "здається", "оренда", "оренду", "аренда", "аренду", "подобово", "погодинно",
    )
    type_markers = {
        "apartment": {
            # A land/house/commercial slug should not count as an apartment
            # match even if it also mentions floors etc.
            "exclude_latin": ("uchastok", "dilyanka", "dilyank", "zemel", "sotok", "sotk", "gektar", "dom-", "-doma", "kottedzh", "dachu", "dachnyy"),
            "exclude_cyrillic": ("ділянк", "земел", "гектар", "будинок", "будинку", "котедж", "дачу", "дачний"),
        },
        "house": {
            "exclude_latin": ("kvartir",),
            "exclude_cyrillic": ("квартир",),
        },
        "land": {
            "exclude_latin": ("kvartir", "kimnat", "komnat", "dom-", "-doma", "kottedzh"),
            "exclude_cyrillic": ("квартир", "кімнат", "комнат", "будинок", "котедж"),
        },
        "commercial": {
            "exclude_latin": ("kvartir",),
            "exclude_cyrillic": ("квартир",),
        },
    }
    markers = type_markers.get(property_type, type_markers["apartment"])

    def matches_type(item):
        if not matches_city(item):
            return False
        slug = str(item.get("url") or "").casefold()
        haystack = (str(item.get("title") or "") + " " + str(item.get("_preview_text") or "")).casefold()
        if any(marker in slug for marker in non_realty_exclude_latin) or \
                any(marker in haystack for marker in non_realty_exclude_cyrillic):
            return False
        if any(marker in slug for marker in rental_exclude_latin) or \
                any(marker in haystack for marker in rental_exclude_cyrillic):
            return False
        if any(marker in slug for marker in markers["exclude_latin"]) or \
                any(marker in haystack for marker in markers["exclude_cyrillic"]):
            return False
        return True

    def score(item):
        if not matches_type(item):
            return -10000.0
        result = 0.0
        preview_rooms = item.get("rooms")
        preview_area = item.get("area_sqm")
        if rooms:
            if preview_rooms is not None and int(preview_rooms) != int(rooms):
                return -10000.0
            result += 45 if preview_rooms is not None else 6
        if area_sqm:
            if preview_area:
                difference = abs(float(preview_area) - float(area_sqm)) / float(area_sqm)
                # Area is a ranking preference here, not a hard cutoff (per
                # product decision): only room count and price stay strict.
                # Closer area still sorts to the top of the detail-scan queue.
                result += max(0.0, 35.0 - difference * 100)
            else:
                result += 5
        haystack = (str(item.get("title") or "") + " " + str(item.get("_preview_text") or "")).casefold()
        if district_text and district_text in haystack:
            result += 18
        if item.get("price_uah"):
            result += 3
        return result

    ranked = sorted(listings, key=score, reverse=True)
    return [item for item in ranked if score(item) > -10000.0]


async def search_olx(city, property_type="apartment", max_pages=2, rooms=None, district=None, target_results=60, start_page=1, region=None):
    all_listings = []
    fetch_attempts = 0
    fetch_failures = 0
    for page in range(start_page, start_page + max_pages):
        if property_type == "apartment" and rooms:
            # Query the Russian and Ukrainian room-count phrasing, plus the
            # plain "продаж {квартиру} {місто}" phrasing that produced a
            # real, verified-good report before OLX's search behaviour
            # started drifting (no room count, no "куплю" — see the "legacy"
            # variant docstring). Merged together; the downstream city/type/
            # rental filters now do the precision work all three used to
            # rely on OLX's own text search for.
            urls = [
                _build_olx_search_url(city, property_type, page, rooms, district, room_word="ком", region=region),
                _build_olx_search_url(city, property_type, page, rooms, district, room_word="кімн", region=region),
                _build_olx_search_url(city, property_type, page, rooms, district, variant="legacy", region=region),
            ]
            for search_url in urls:
                print("OLX search page " + str(page) + ": " + search_url)
            # The three phrasings are fetched one after another, not together.
            #
            # Firing them concurrently put three ZenRows calls in the same
            # second, and the detail scanner adds three more, so a single
            # search could hold six provider connections at once. On a plan
            # with a low concurrency allowance that returns 429 for all of
            # them: a real run logged three 429s within one second and
            # collected nothing, while the same search worked when the calls
            # were spread out.
            #
            # The cost is a few seconds per page, which is far cheaper than
            # losing the whole source.
            htmls = []
            for url in urls:
                htmls.append(await _fetch_page(url))
                await asyncio.sleep(0.8)
            fetch_attempts += len(htmls)
            fetch_failures += sum(1 for html in htmls if not html)
            page_listings = []
            for html in htmls:
                if html:
                    page_listings.extend(_parse_search_page(html, 30))
            if not any(htmls):
                break
        else:
            search_url = _build_olx_search_url(city, property_type, page, rooms, district, region=region)
            print("OLX search page " + str(page) + ": " + search_url)
            html = await _fetch_page(search_url)
            fetch_attempts += 1
            if not html:
                fetch_failures += 1
                break
            page_listings = _parse_search_page(html, 30)
        print("OLX page " + str(page) + ": " + str(len(page_listings)) + " found")
        all_listings.extend(page_listings)
        # Two full OLX pages normally provide the required 30–60-candidate sample.
        if len(all_listings) >= target_results:
            break
        if len(page_listings) < 5:
            break
        # Random delay between pages
        await asyncio.sleep(random.uniform(1.5, 3.0))

    seen = set()
    unique = []
    for item in all_listings:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    print("OLX total unique: " + str(len(unique)))
    if not unique and fetch_attempts > 0 and fetch_failures == fetch_attempts:
        # Every single page fetch failed at the transport layer (bad/expired
        # ZenRows key, exhausted quota, network error, OLX block) rather than
        # succeeding with zero matching cards. Left unmarked this looked
        # identical to "the market genuinely has nothing here" in the
        # appraiser-facing status ("OLX: ok (0)"). Raise so the caller records
        # a real "error" status instead of a silent empty result.
        raise RuntimeError(
            f"OLX: усі {fetch_attempts} спроб(и) отримати сторінку не вдались "
            "(ZenRows/ScraperAPI). Перевірте ключ і ліміт запитів провайдера."
        )
    return unique


async def get_listing_details(url, usd_rate=41.5):
    html = await _fetch_page(url)
    if not html:
        return None
    try:
        title = ""
        tm = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
        if tm:
            title = html_lib.unescape(tm.group(1)).strip()[:300]
        if not title:
            og = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.IGNORECASE)
            if og:
                title = html_lib.unescape(og.group(1)).strip()[:300]
        if not title:
            slug = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
            title = re.sub(r'-ID[^-]+\.html$', '', slug, flags=re.IGNORECASE).replace('-', ' ').strip()[:300]

        price = _extract_olx_price(html, title, usd_rate)
        area = None
        am = re.search(r'(?:Загальна площа|Площа)[:\s]*(\d+[.,]?\d*)', html, re.IGNORECASE)
        if am:
            try:
                area = float(am.group(1).replace(",", "."))
            except ValueError:
                pass
        rooms = None
        rm = re.search(r'(?:Кількість кімнат|Кімнат)[:\s]*(\d+)', html, re.IGNORECASE)
        if rm:
            try:
                rooms = int(rm.group(1))
            except ValueError:
                pass
        floor = None
        fm = re.search(r'Поверх[:\s]*(\d+)', html, re.IGNORECASE)
        if fm:
            try:
                floor = int(fm.group(1))
            except ValueError:
                pass
        total_floors = None
        tfm = re.search(r'(?:Поверховість|Кількість поверхів|Этажность)[:\s]*(\d+)', html, re.IGNORECASE)
        if tfm:
            try:
                total_floors = int(tfm.group(1))
            except ValueError:
                pass
        # Fallback parser for current OLX pages: facts may be rendered as
        # compact JSON or as Russian/Ukrainian visible labels.  Missing rooms
        # previously caused valid cards to be rejected by the strict filter.
        page_text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html))
        page_text = re.sub(r"\s+", " ", page_text)
        source_text = f"{title} {page_text}"

        def _number(patterns, integer=False):
            for pattern in patterns:
                match = re.search(pattern, source_text, re.IGNORECASE)
                if not match:
                    continue
                try:
                    value = match.group(1).replace(",", ".")
                    return int(value) if integer else float(value)
                except (TypeError, ValueError):
                    continue
            return None

        if area is None:
            area = _number([
                r"(?:Загальна\s+площа|Общая\s+площадь|Площа|Площадь)\s*[:\-]?\s*(\d{2,3}(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м)?",
                r"(\d{2,3}(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м)",
            ])

        # v2: Extended area fallback runs UNCONDITIONALLY when the standard
        # parser above did not find area. The previous v1 gate (is_house by
        # URL/title match) was too narrow — many house URLs did not match
        # any of the tried substrings, so the fallback never ran. This
        # version relies on the plot-area mask + sane-range guard (20-500 m²)
        # to avoid mis-reading плот/ділянка as building area, and does not
        # affect apartments (whose area is virtually always found by the
        # standard structured-label parser above, so this block never runs
        # for them in practice).
        if area is None:
            # Mask plot-area mentions before scanning, so 12 соток / 5 га /
            # ділянка cannot be picked up as building area.
            text_no_land = re.sub(
                r"\d+[.,]?\d*\s*(?:сот[а-яії]*|га\b|гектар[а-яії]*|ділянк[а-яії]*)",
                " ", source_text, flags=re.IGNORECASE,
            )

            def _house_area_number(patterns):
                for pattern in patterns:
                    m = re.search(pattern, text_no_land, re.IGNORECASE)
                    if not m:
                        continue
                    try:
                        val = float(m.group(1).replace(",", "."))
                        if 20 <= val <= 500:
                            return val
                    except (TypeError, ValueError):
                        continue
                return None

            area = _house_area_number([
                # "будинок 90/45/12" — total/living/kitchen shorthand
                r"будин\w*[^\d]{0,40}(\d{2,3}(?:[.,]\d+)?)\s*[/\\]\s*\d",
                # "дом 90/45/12" (RU)
                r"дом\w*[^\d]{0,40}(\d{2,3}(?:[.,]\d+)?)\s*[/\\]\s*\d",
                # "загальна площа будинку: 90" / "площа будинку 90"
                r"(?:загальна\s+площа\s+будинку|площа\s+будинку)\s*[:\-]?\s*(\d{2,3}(?:[.,]\d+)?)",
                # "загальна площадь дома" (RU)
                r"(?:общая\s+площадь\s+дома|площадь\s+дома)\s*[:\-]?\s*(\d{2,3}(?:[.,]\d+)?)",
                # "будинок 90 м" or "будинок 90 кв" — number right after
                r"будин\w*[^\d]{0,25}(\d{2,3}(?:[.,]\d+)?)\s*(?:м|кв|квадрат)",
                # "дом 90 м" or "дом 90 кв" (RU)
                r"дом\w*[^\d]{0,25}(\d{2,3}(?:[.,]\d+)?)\s*(?:м|кв|квадрат)",
                # "90 квадратних метрів" / "90 кв метрів" (already excludes land via mask)
                r"(\d{2,3}(?:[.,]\d+)?)\s*(?:квадратн\w*|кв\.?\s*метр\w*)",
                # "площа: 90" but NOT if followed by сот/га/ділянк/etc
                r"(?:^|\s)(?:загальна\s+)?площа\s*[:\-]\s*(\d{2,3}(?:[.,]\d+)?)(?!\s*(?:сот|га|ділян))",
                # RU "площадь: 90"
                r"(?:^|\s)(?:общая\s+)?площадь\s*[:\-]\s*(\d{2,3}(?:[.,]\d+)?)(?!\s*(?:сот|га|участк))",
            ])
            if area:
                print("OLX extended-area fallback matched: " + str(area) + " m² for " + url)
            else:
                # Diagnostic: print a short preview of the text so we can see
                # what really appears in these listings when nothing matches.
                # Cap by module-level counter to avoid log spam.
                try:
                    globals().setdefault("_area_debug_count", 0)
                    if globals()["_area_debug_count"] < 3:
                        globals()["_area_debug_count"] += 1
                        preview = source_text[:300].replace("\n", " ")
                        print("OLX no_area DEBUG [" + str(globals()["_area_debug_count"]) + "/3]: url=" + url)
                        print("OLX no_area DEBUG text=" + preview)
                except Exception:
                    pass
        if rooms is None:
            rooms = _number([
                r"(?:Кількість\s+кімнат|Кімнат|Комнат)\s*[:\-]?\s*([1-9])",
                r"(?:^|\s)([1-9])\s*[-–]?\s*(?:кімнатн\w*|комнатн\w*|к\.)",
            ], integer=True)
        if floor is None:
            floor = _number([
                r"(?:Поверх|Этаж)\s*[:\-]?\s*(\d{1,2})(?:\s*(?:з|/|из)\s*\d{1,2})?",
            ], integer=True)
        if total_floors is None:
            total_floors = _number([
                r"(?:Поверховість|Кількість\s+поверхів|Этажность|Этажей)\s*[:\-]?\s*(\d{1,2})",
                r"(?:Поверх|Этаж)\s*[:\-]?\s*\d{1,2}\s*(?:з|/|из)\s*(\d{1,2})",
            ], integer=True)

        ppsm = None
        if price and area and area > 0:
            ppsm = round(price / area)
        return {"source": "olx", "url": url, "title": title,
                "price_uah": price, "area_sqm": area, "rooms": rooms,
                "floor": floor, "total_floors": total_floors, "price_per_sqm": ppsm}
    except Exception as e:
        print("OLX detail parse error: " + str(e))
        return None


async def find_olx_analogs(city, property_type="apartment", rooms=None, area_sqm=None,
                            report_id="", max_pages=5, max_candidates=60, screenshots=False, district=None,
                            price_min_uah=None, price_max_uah=None, region=None):
    """Collect a market sample first; selection is done by valuation_analytics."""
    usd_rate = await get_usd_rate()
    # First inspect only the first category page.  A second page is opened
    # only if the first sample contains too few exact cards.  This makes the
    # request count predictable and avoids spending provider credits on every
    # advertised object in the city.
    # Twelve verified candidates normally give the appraiser a genuine choice
    # of 3–5 final analogues across the lower, middle and upper market
    # segments. This is a target, never a reason to loosen room/area rules.
    target_strict_matches = min(12, max_candidates)
    # Read up to 60 listing cards.  A normal OLX page has about 30 cards, but
    # thinner categories can have 20; allow a third listing page before
    # deciding that the market genuinely has too few comparables.  Detail
    # pages remain capped below, so this does not turn into 60 paid card opens.
    initial_pages = min(3, max_pages)
    raw = await search_olx(city, property_type, max_pages=initial_pages, rooms=rooms, district=district, target_results=min(60, max_candidates), region=region)
    # Some towns do not have a stable district spelling in OLX.  Relax only
    # the query wording when it produced almost no cards; exact rooms and
    # area are still enforced after individual-card parsing below.
    if len(raw) < target_strict_matches and district:
        print("OLX exact district query returned too few cards; trying city and rooms")
        raw = await search_olx(city, property_type, max_pages=initial_pages, rooms=rooms, target_results=min(60, max_candidates), region=region)
    if len(raw) < target_strict_matches and rooms:
        print("OLX room query returned too few cards; trying the real-estate city category")
        raw = await search_olx(city, property_type, max_pages=initial_pages, target_results=min(60, max_candidates), region=region)
    if not raw:
        return []

    # Search-page metadata is free compared with opening individual cards.
    # Keep the query unchanged and use that metadata only to prioritise which
    # URLs deserve detailed verification.
    raw = _prioritize_listings(raw, rooms=rooms, area_sqm=area_sqm, district=district, property_type=property_type, city=city)
    print("OLX list prefilter: " + str(len(raw)) + " viable cards")

    # Do not serially wait 1–2.5 seconds for every card: 60 listings then take
    # several minutes. A small bounded pool is fast enough for a user request
    # while remaining polite to the scraper provider and OLX.
    # A bounded pool keeps the 48-card review responsive without opening an
    # uncontrolled number of proxy sessions at once.
    # ZenRows rejects bursts of OLX detail requests with HTTP 429. Three at a
    # time was still too many once the search phase was also running three
    # concurrent calls: one real run had every card come back rejected as
    # "no_data" -- forty-two of them -- because the provider was refusing the
    # detail requests, not because the listings were bad. Two leaves room
    # under the same allowance.
    concurrency = 2
    semaphore = asyncio.Semaphore(concurrency)

    # Rejection categories split from the former single "unavailable_or_bad_price":
    #   no_data          — detail scraper returned None (page dead/blocked)
    #   no_area          — area_sqm missing (parser could not read it)
    #   no_price         — price_uah is 0 or missing
    #   below_min_price  — price < MIN_MARKET_PRICE_UAH (5,000 UAH — likely
    #                      a parser artefact treating an area value as price)
    # Kept as 4 separate counters so the next report log tells us exactly
    # what is really being lost, instead of one opaque bucket for 58 cards.
    rejection_counts = {"no_data": 0, "no_area": 0, "no_price": 0, "below_min_price": 0, "rooms": 0, "area": 0, "price_corridor": 0}

    async def fetch_detail(index, listing):
        async with semaphore:
            # Stagger each batch slightly instead of a multi-second delay per card.
            await asyncio.sleep((index % concurrency) * 0.35)
            try:
                return await asyncio.wait_for(get_listing_details(listing["url"], usd_rate), timeout=14)
            except asyncio.TimeoutError:
                print("OLX detail timed out after 14s: " + str(listing.get("url") or ""))
                return None

    detailed = []

    def eligible(detail):
        # Values such as 46 or 55 are a parser artefact, not a real-estate
        # asking price.  Reject them here as a last source-level safeguard,
        # before they can affect the candidate pool or price segments.
        # Split from a single "unavailable_or_bad_price" check into four
        # discrete reasons so the strict-filter log points to the real cause.
        if not detail:
            rejection_counts["no_data"] += 1
            return False
        if not detail.get("area_sqm"):
            rejection_counts["no_area"] += 1
            return False
        price_value = float(detail.get("price_uah") or 0)
        if price_value <= 0:
            rejection_counts["no_price"] += 1
            return False
        if price_value < MIN_MARKET_PRICE_UAH:
            rejection_counts["below_min_price"] += 1
            return False
        try:
            if rooms and int(detail.get("rooms")) != int(rooms):
                rejection_counts["rooms"] += 1
                return False
        except (TypeError, ValueError):
            rejection_counts["rooms"] += 1
            return False
        # Area is intentionally NOT a hard cutoff here (per product decision):
        # only room count (exact) and price corridor (±25%) are enforced as
        # rejections. Area still needs to exist and be a sane number (guarded
        # above), and valuation_analytics still uses it for weighting/segment
        # placement of accepted candidates — it just no longer disqualifies
        # an otherwise-matching, correctly-priced listing.
        try:
            price = float(detail.get("price_uah") or 0)
            if price_min_uah is not None and price < float(price_min_uah):
                rejection_counts["price_corridor"] += 1
                if rejection_counts["price_corridor"] <= 8:
                    print(f"OLX price-corridor reject (too low): {price:,.0f} грн < {float(price_min_uah):,.0f} — {detail.get('url','')}")
                return False
            if price_max_uah is not None and price > float(price_max_uah):
                rejection_counts["price_corridor"] += 1
                if rejection_counts["price_corridor"] <= 8:
                    print(f"OLX price-corridor reject (too high): {price:,.0f} грн > {float(price_max_uah):,.0f} — {detail.get('url','')}")
                return False
        except (TypeError, ValueError):
            rejection_counts["price_corridor"] += 1
            return False
        return True

    async def scan(listings, offset=0):
        if not listings:
            return []
        print(f"OLX detail scan: {len(listings)} listings, concurrency {concurrency}")
        details = await asyncio.gather(
            *(fetch_detail(offset + index, listing) for index, listing in enumerate(listings)),
            return_exceptions=True,
        )
        result = []
        for detail in details:
            if isinstance(detail, Exception):
                print("OLX detail request error: " + str(detail))
                continue
            if eligible(detail):
                result.append(detail)
        return result

    # The search pages are the broad 30–60-item market sample.  Details are
    # paid provider calls, so use short batches and stop as soon as enough
    # complete strict matches exist for a 10–12-item candidate review.  The
    # Start with the best 30 cards from the already fetched result pages.
    # Only then expand to 60 if the strict room/price check still has too few
    # candidates. A real run's accept rate after room/price/type/rental
    # filtering was as low as ~10-20%, so a 20/36 budget routinely undershot
    # the 10-12 candidates an appraiser actually wants to choose from.
    primary_detail_budget = min(len(raw), 30)
    detail_budget = min(len(raw), 60)
    batch_size = 6
    for offset in range(0, primary_detail_budget, batch_size):
        detailed.extend(await scan(raw[offset:offset + batch_size], offset=offset))
        if len(detailed) >= target_strict_matches:
            break

    if len(detailed) < target_strict_matches:
        print("OLX initial shortlist below twelve; extending detailed verification")
        for offset in range(primary_detail_budget, detail_budget, batch_size):
            detailed.extend(await scan(raw[offset:offset + batch_size], offset=offset))
            if len(detailed) >= target_strict_matches:
                break

    # Do not pay for deep pagination in an ordinary case. If the first 40–60
    # category cards produced fewer than twelve exact room/area matches, extend
    # the same city query by more pages. The fallback preserves the identical
    # comparability rules; it is not a price-driven search and therefore
    # cannot replace a broad market sample with adverts chosen for a target
    # value.
    if len(detailed) < target_strict_matches and max_pages > initial_pages:
        print("OLX strict sample below twelve; extending listing search")
        # The extension is a best-effort top-up, never a precondition. It runs
        # in its own try/except because search_olx RAISES when every fetch
        # attempt fails (exhausted provider credits, a 402/429, a blocked
        # page). That exception used to propagate out of find_olx_analogs and
        # be caught by analog_search's per-source handler, which logged
        # "Analog source failed: olx" and returned an empty list -- throwing
        # away every candidate already verified from pages 1-2. A real run
        # collected 45 viable cards, verified them, then lost all of them
        # because page 4 came back 402 Payment Required.
        try:
            extra = await search_olx(
                city, property_type, max_pages=max_pages - initial_pages, rooms=rooms,
                district=district, target_results=min(100, max_candidates), start_page=initial_pages + 1, region=region,
            )
        except Exception as extension_error:
            print(
                "OLX extended listing search failed, keeping the "
                + str(len(raw)) + " cards already collected: " + str(extension_error)
            )
            extra = []
        seen_urls = {str(item.get("url") or "") for item in raw}
        extra = [item for item in extra if str(item.get("url") or "") not in seen_urls]
        # This batch bypassed _prioritize_listings entirely in a previous
        # version — it went straight into detail-scanning, which is exactly
        # how a school backpack, a POS terminal and kittens ended up in a
        # real run's "OLX detail scan" queue. Apply the same type/quality
        # filter as the primary batch before spending any detail requests on it.
        extra = _prioritize_listings(extra, rooms=rooms, area_sqm=area_sqm, district=district, property_type=property_type, city=city)
        print("OLX extended listing search: " + str(len(extra)) + " viable cards")
        # A limited extra detail budget keeps the fallback bounded.  Stop as
        # soon as the target set of twelve exact cards is collected.
        for offset in range(0, min(len(extra), 40), batch_size):
            detailed.extend(await scan(extra[offset:offset + batch_size], detail_budget + offset))
            if len(detailed) >= target_strict_matches:
                break
    print("OLX strict filter: accepted=" + str(len(detailed)) + ", rejected=" + str(rejection_counts) + ", criteria rooms=" + str(rooms) + ", area=" + str(area_sqm) + ", price=" + str(price_min_uah) + ".." + str(price_max_uah))
    # Statistical safety net for cross-city contamination the keyword city
    # filter cannot catch: a listing can legitimately omit any city name and
    # be identified only by a neighbourhood/complex name (e.g. "ЖК Щасливий",
    # "Позняки" — both Kyiv-only, unrecognisable without a landmark
    # database). A real run mixed such Kyiv cards (3.6-4.9M грн) into a
    # Slov'yansk search (real local cards ~250-580k грн) purely because OLX's
    # own search geography drifted. Price-per-m² for the same room count in
    # one city does not plausibly vary several-fold; drop anything that
    # diverges more than 3x from this batch's own median as an outlier
    # rather than trust city-name text matching alone.
    ppsm_values = sorted(d["price_per_sqm"] for d in detailed if d.get("price_per_sqm"))
    if len(ppsm_values) >= 3:
        median_ppsm = ppsm_values[len(ppsm_values) // 2]
        before = len(detailed)
        detailed = [
            d for d in detailed
            if not d.get("price_per_sqm") or (median_ppsm / 3 <= d["price_per_sqm"] <= median_ppsm * 3)
        ]
        if len(detailed) < before:
            print(f"OLX outlier price/m2 removed: {before - len(detailed)} (median {median_ppsm:.0f} грн/м²)")
    print("OLX candidates collected: " + str(len(detailed)))
    if screenshots:
        for i, analog in enumerate(detailed, 1):
            ss_dir = os.path.join(settings.screenshots_dir, report_id)
            ss_path = os.path.join(ss_dir, "olx_" + str(i) + ".png")
            ok = await _take_screenshot(analog["url"], ss_path)
            if ok:
                analog["screenshot_path"] = ss_path
    return detailed
