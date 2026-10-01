"""Search orchestration shared by OLX and DIM.RIA."""
from __future__ import annotations
import asyncio
from time import perf_counter
from typing import Any


def build_search_variants(city: str, object_type: str, rooms: int | None = None, area_sqm: float | None = None, district: str | None = None) -> list[str]:
    labels = {"apartment": "квартира", "house": "будинок", "land": "земельна ділянка"}
    label = labels.get(str(object_type), "нерухомість")
    rooms_text = f" {rooms}-кімнатна" if rooms and object_type == "apartment" else ""
    area_text = f" {round(area_sqm)} м²" if area_sqm else ""
    place = f" {district}" if district else ""
    # Exact -> broad. These are stored in the journal for reproducibility.
    return [
        f"продаж {label}{rooms_text} {city}{place}{area_text}".strip(),
        f"продаж {label}{rooms_text} {city}{place}".strip(),
        f"продаж {label} {city}".strip(),
    ]


async def search_all(
    subject: dict[str, Any], settings: Any, include_olx: bool = True,
    price_min_uah: float | None = None, price_max_uah: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.services.dimria_service import search_analogs
    from app.services.olx_service import find_olx_analogs
    city = subject["city"]
    object_type = str(subject.get("object_type") or "apartment")
    journal = {
        "queries": build_search_variants(city, object_type, subject.get("rooms"), subject.get("area_sqm"), subject.get("district")),
        "sources": {},
        "e_certificate_price_filter": {
            "active": price_min_uah is not None and price_max_uah is not None,
            "minimum_uah": price_min_uah,
            "maximum_uah": price_max_uah,
        },
        "search_order": ["dimria"] + (["olx"] if include_olx else []),
        "collection_policy": "DIM.RIA: до 20 карток API; OLX: вибірка з перших 1–2 сторінок, а за менш ніж 10 точних збігів — продовження до сторінки 3. Перевірка карток короткими серіями з достроковою зупинкою; скриншоти лише для фінальних аналогів.",
    }
    async def collect(source: str, request, timeout_seconds: int) -> list[dict[str, Any]]:
        """Keep one slow marketplace from holding the other one hostage."""
        started = perf_counter()
        try:
            result = await asyncio.wait_for(request, timeout=timeout_seconds)
            journal["sources"][source] = len(result)
            journal["sources"][f"{source}_status"] = "ok"
            return result
        except asyncio.TimeoutError:
            journal["sources"][source] = 0
            journal["sources"][f"{source}_status"] = "timed_out"
            print(f"Analog source timed out: {source} after {timeout_seconds}s", flush=True)
            return []
        except Exception as error:
            journal["sources"][source] = 0
            journal["sources"][f"{source}_status"] = "error"
            journal["sources"][f"{source}_error"] = str(error)[:220]
            print(f"Analog source failed: {source}: {error}", flush=True)
            return []
        finally:
            journal["sources"][f"{source}_seconds"] = round(perf_counter() - started, 1)

    tasks: dict[str, Any] = {}
    # DIM.RIA remains the primary source in the journal and result ordering.
    # Fetching it concurrently with OLX only removes an unnecessary wait when
    # city resolution or an external quota response is slow.
    if settings.dimria_enabled:
        tasks["dimria"] = collect(
            "dimria",
            search_analogs(
                city=city,
                property_type=object_type,
                rooms=subject.get("rooms"),
                area_sqm=subject.get("area_sqm"),
                district=subject.get("district"),
                region=subject.get("region"),
                max_results=20,
            ),
            # 75s, raised from 35s.
            #
            # Detail pages are now fetched one at a time with a pause between
            # them, because firing them concurrently made DIM.RIA answer 429
            # to every one. That deliberate pacing costs roughly 0.6s of
            # waiting plus the request itself per listing, so twenty listings
            # no longer fit in 35 seconds and the source was being cut off
            # mid-way: a real run logged "dimria after 35s" while OLX went on
            # to return ten candidates alone.
            timeout_seconds=75,
        )
    else:
        journal["sources"]["dimria"] = 0
        journal["sources"]["dimria_status"] = "disabled"

    if include_olx:
        # The OLX adapter reads a broad search sample, then opens card pages
        # in bounded batches and stops as soon as enough exact candidates are
        # available. Evidence screenshots are deliberately NOT collected here:
        # they are an optional, later step for only the final five cards.
        tasks["olx"] = collect(
            "olx",
            find_olx_analogs(
                city, object_type, subject.get("rooms"), subject.get("area_sqm"),
                # Up to 100 listing cards / 90 detailed cards. A real-world
                # accept rate after room/price/type/rental filtering has been
                # observed as low as ~10-20%, so reaching a comfortable 10-12
                # accepted candidates needs a meaningfully larger raw pool
                # than the 60/48 figures used earlier.
                report_id="", max_pages=6, max_candidates=100, screenshots=False,
                district=subject.get("district"),
                price_min_uah=price_min_uah,
                price_max_uah=price_max_uah,
                region=subject.get("region"),
            ),
            # The adapter stops after the first two pages whenever it already
            # has ten strict matches. Page 3 is a fallback only when fewer
            # than ten comparable cards were verified.
            # Individual OLX cards fail fast in the adapter. Keep the source
            # deadline long enough to return the remaining valid cards instead
            # of discarding all OLX results after one slow provider batch.
            # Raised alongside the larger candidate pool above (more pages,
            # more detail-scan batches) so this deadline doesn't cut the
            # search off before it can reach the requested candidate count.
            timeout_seconds=260,
        )
    else:
        journal["sources"]["olx"] = 0
        journal["sources"]["olx_status"] = "not_selected_by_user"

    collected = await asyncio.gather(*tasks.values()) if tasks else []
    by_source = dict(zip(tasks.keys(), collected))
    dimria = by_source.get("dimria", [])
    olx = by_source.get("olx", [])
    combined = dimria + olx

    # Фільтр цінових викидів. Об'єднаний пул містить і DIM.RIA, і OLX;
    # часом одне джерело віддає одиничне оголошення з ціною далеко від
    # ринкової медіани (застаріле, преміум-ремонт, помилка продавця).
    # Відсіюємо такі до того, як їх побачить оцінювач і LLM.
    # Поріг +/-35% обраний свідомо ширшим за ФДМУ-коридор +/-25%:
    # мета — прибрати явні викиди, а не звузити вибірку.
    filtered, filter_info = _drop_price_outliers(combined, tolerance=0.35, min_keep=5)
    journal["price_outlier_filter"] = filter_info
    return filtered, journal


def _drop_price_outliers(items, tolerance=0.35, min_keep=5):
    """Прибрати аналоги, чия ціна/м2 відхиляється від медіани більш ніж на tolerance.

    Правила безпеки:
      • якщо позицій менше ніж min_keep — не фільтруємо взагалі (мало даних);
      • якщо після фільтру залишилося менше ніж min_keep — не фільтруємо
        (краще неідеальний набір, ніж занадто малий: оцінювач бачить все,
         і сам вирішує який брати);
      • позиції без ціни або площі залишаються — щоб не втратити
        оголошення яких ще не встиг розпарсити пайплайн.
    """
    def _num(value):
        try:
            v = float(value)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    def _ppsqm(item):
        area = _num(item.get("area_sqm") or item.get("area") or item.get("total_area"))
        price = _num(item.get("price_uah") or item.get("price"))
        if not area or not price:
            return None
        return price / area

    info = {"input_count": len(items), "removed": [], "median_price_per_sqm": None}
    if len(items) < min_keep:
        info["reason"] = "too_few_items"
        return items, info

    ppsqm_values = [p for p in (_ppsqm(x) for x in items) if p is not None]
    if len(ppsqm_values) < min_keep:
        info["reason"] = "too_few_priced_items"
        return items, info

    ppsqm_values.sort()
    n = len(ppsqm_values)
    median = ppsqm_values[n // 2] if n % 2 else (ppsqm_values[n // 2 - 1] + ppsqm_values[n // 2]) / 2
    lower = median * (1 - tolerance)
    upper = median * (1 + tolerance)
    info["median_price_per_sqm"] = round(median, 1)
    info["bounds_uah_per_sqm"] = [round(lower, 1), round(upper, 1)]

    kept = []
    removed = []
    for item in items:
        pp = _ppsqm(item)
        if pp is None:
            kept.append(item)  # без ціни/площі не судимо
            continue
        if lower <= pp <= upper:
            kept.append(item)
        else:
            removed.append({
                "source": item.get("source"),
                "url": item.get("url") or item.get("link"),
                "price_uah": item.get("price_uah") or item.get("price"),
                "area_sqm": item.get("area_sqm") or item.get("area"),
                "price_per_sqm": round(pp, 1),
                "reason": "below_median" if pp < lower else "above_median",
            })

    if len(kept) < min_keep:
        info["reason"] = "filter_would_leave_too_few_kept_all"
        return items, info

    info["kept_count"] = len(kept)
    info["removed_count"] = len(removed)
    info["removed"] = removed
    return kept, info
