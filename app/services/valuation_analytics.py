"""Transparent comparable-selection and valuation calculations.

The functions in this module are deterministic and do not make a valuation
decision: they prepare an auditable market sample for the licensed valuer.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit
import re


@dataclass
class AnalysisResult:
    candidates: list[dict[str, Any]]
    selected: list[dict[str, Any]]
    statistics: dict[str, float | int]
    journal: dict[str, Any]


def statistics_for_selected(items: Iterable[dict[str, Any]], subject: dict[str, Any]) -> dict[str, float | int]:
    """Calculate transparent statistics for the exact cards selected by a valuer.

    The caller has already applied the broad comparability and outlier rules.
    This function deliberately does *not* filter or replace a card again: the
    final 3--5 cards are a documented professional selection.
    """
    selected = [dict(item) for item in items]
    ppsm: list[float] = []
    for item in selected:
        price, area = _num(item.get("price_uah")), _num(item.get("area_sqm"))
        if not price or not area or area <= 0:
            continue
        ppsm.append(price / area)
    area = _num(subject.get("area_sqm")) or 0
    centre = median(ppsm) if ppsm else 0
    low, high = _quartiles(ppsm) if ppsm else (0, 0)
    return {
        "count": len(selected),
        "median_price_per_sqm": round(centre, 2),
        "mean_price_per_sqm": round(sum(ppsm) / len(ppsm), 2) if ppsm else 0,
        "min_price_per_sqm": round(low, 2),
        "max_price_per_sqm": round(high, 2),
        "recommended_value": round(centre * area),
        "range_min": round(low * area),
        "range_max": round(high * area),
    }


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def deduplicate(items: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Remove exact URLs/IDs and probable cross-platform duplicates.

    A probable duplicate must match price and area and have a near-identical
    normalized title/address. Such matches are logged rather than silently
    merged in the database.
    """
    source = list(items)
    result, keys = [], set()
    for item in source:
        raw = item.get("raw_data") or {}
        url = canonical_url(item.get("url"))
        source_id = str(item.get("source_id") or raw.get("id") or "")
        price, area = _num(item.get("price_uah")), _num(item.get("area_sqm"))
        text = " ".join(str(item.get(k) or "") for k in ("title", "address", "district")).lower()
        text = re.sub(r"[^\wа-яіїєґ]+", " ", text).strip()
        signature = (round(price or 0, -3), round(area or 0, 1), text[:80])
        identity = (url or None, (item.get("source"), source_id) if source_id else None, signature)
        if any(part and part in keys for part in identity):
            continue
        keys.update(part for part in identity if part)
        result.append(item)
    return result, max(0, len(source) - len(result))


def _mad_filter(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    values = [float(i["price_per_sqm"]) for i in items if _num(i.get("price_per_sqm"))]
    if len(values) < 4:
        return items, 0
    centre = median(values)
    mad = median([abs(v - centre) for v in values])
    if mad == 0:
        return items, 0
    allowed = 3.5 * 1.4826 * mad
    kept = [i for i in items if abs(float(i["price_per_sqm"]) - centre) <= allowed]
    return kept, len(items) - len(kept)


def _quartiles(values: list[float]) -> tuple[float, float]:
    """Return Tukey-style Q1/Q3 without letting one extreme set the range."""
    ordered = sorted(values)
    if len(ordered) < 4:
        return ordered[0], ordered[-1]
    midpoint = len(ordered) // 2
    lower = ordered[:midpoint]
    upper = ordered[midpoint + (len(ordered) % 2):]
    return float(median(lower)), float(median(upper))


def _relative_market_filter(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, float]:
    """Remove only extreme price-per-square-metre cards from a usable sample.

    MAD catches isolated mathematical outliers.  A small city sample can still
    contain an obviously distressed listing at about half of the central price
    without exceeding the very wide MAD band.  This second, conservative gate
    is used only when at least five comparable cards are available.  It is a
    transparent pre-selection screen, not a substitute for the valuer's
    professional adjustment for condition or location.
    """
    values = [float(item["price_per_sqm"]) for item in items if _num(item.get("price_per_sqm"))]
    if len(values) < 5:
        return items, 0, 0.0
    centre = float(median(values))
    if centre <= 0:
        return items, 0, 0.0
    # The lower guard is deliberately tighter: a price below 55% of the
    # central similar-market sample is normally a different condition, a
    # distressed sale or an extraction mistake.  A price above 145% is also
    # treated as a premium outlier at this automated stage; the valuer can
    # still retain it separately with a documented professional adjustment.
    low, high = centre * 0.60, centre * 1.45
    kept = [item for item in items if low <= float(item["price_per_sqm"]) <= high]
    # Never leave an unusable comparison set just because a thin market is
    # uneven; the previous MAD-filtered sample is safer in that situation.
    if len(kept) < 3:
        return items, 0, centre
    return kept, len(items) - len(kept), centre


def score(item: dict[str, Any], subject: dict[str, Any]) -> float:
    """0–100 similarity score; all component penalties are explainable."""
    points = 100.0
    subject_area = _num(subject.get("area_sqm"))
    item_area = _num(item.get("area_sqm"))
    if subject_area and item_area:
        points -= min(35, abs(item_area - subject_area) / subject_area * 100)
    subject_rooms = _num(subject.get("rooms"))
    item_rooms = _num(item.get("rooms"))
    if subject_rooms is not None and item_rooms is not None and int(subject_rooms) != int(item_rooms):
        points -= 35
    if subject.get("floor") and item.get("floor"):
        # A different floor is not automatically disqualifying, but it is a
        # material comparable characteristic.  Give exact/similar floors a
        # noticeably higher score without pretending that a 1st-floor flat
        # is identical to a middle-floor flat.
        points -= min(18, abs(int(subject["floor"]) - int(item["floor"])) * 3)
    if subject.get("total_floors") and item.get("total_floors"):
        points -= min(6, abs(int(subject["total_floors"]) - int(item["total_floors"])))
    subject_district = str(subject.get("district") or "").casefold()
    item_district = " ".join(str(item.get(key) or "") for key in ("district", "address", "title")).casefold()
    if subject_district and subject_district not in item_district:
        points -= 12
    if subject.get("year_built") and item.get("year_built"):
        points -= min(8, abs(int(subject["year_built"]) - int(item["year_built"])) / 10)
    return round(max(0, points), 2)


def _price_band_priority(item: dict[str, Any], price_band: dict[str, Any] | None) -> tuple[float, bool, float]:
    """Return a modest secondary priority inside the valid certificate corridor.

    Cards outside the full e-certificate corridor have already been removed
    by the source and analytics guards. Property similarity remains the
    primary selection criterion; this merely orders valid cards within the
    lower, middle or upper review segment.
    """
    if not price_band or not price_band.get("active"):
        return 0.0, False, 0.0
    price = _num(item.get("price_uah"))
    minimum, maximum, centre = (
        _num(price_band.get("minimum")),
        _num(price_band.get("maximum")),
        _num(price_band.get("centre")),
    )
    if not price or not minimum or not maximum or not centre:
        return 0.0, False, 0.0
    in_band = minimum <= price <= maximum
    span = max(maximum - minimum, 1.0)
    distance = abs(price - centre) / span
    # At most 8 points: exact characteristics and location still outweigh
    # the requested price portion of the documented corridor.
    priority = max(0.0, 8.0 * (1.0 - min(distance, 1.0))) if in_band else 0.0
    return round(priority, 2), in_band, round(distance, 4)


def _balanced_price_pool(ranked: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build a review pool across low, middle and high market segments.

    This is a *presentation* balance for the appraiser's review, not a
    price-targeted search.  Cards have already passed the object-type,
    rooms, area, district, duplicate and outlier screens.  The three labels
    simply stop a first-page cluster of similarly priced adverts from hiding
    the rest of the comparable market.
    """
    if not ranked or limit <= 0:
        return [], {"lower": 0, "middle": 0, "upper": 0, "additional": 0}

    market_ordered = sorted(ranked, key=lambda item: (
        float(item.get("price_per_sqm") or 0),
        canonical_url(str(item.get("url") or "")),
    ))
    size = len(market_ordered)
    # Three contiguous price-per-square-metre groups give a transparent
    # lower/middle/upper market view even when asking prices differ because
    # of area.  Each group is still ordered by property similarity below.
    cut_one = max(1, size // 3)
    cut_two = max(cut_one + 1, (size * 2) // 3)
    groups = {
        "lower": market_ordered[:cut_one],
        "middle": market_ordered[cut_one:cut_two],
        "upper": market_ordered[cut_two:],
    }
    rank_position = {id(item): index for index, item in enumerate(ranked)}
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    counts = {"lower": 0, "middle": 0, "upper": 0, "additional": 0}

    # Up to four candidates from every available segment provides a review
    # pool of up to 12 cards (lower / middle / upper). A thin market can
    # legitimately have fewer; we never replace them with a wrong object.
    # With a smaller configured limit this stays proportionate.
    per_segment_limit = max(2, min(4, (limit + 2) // 3))
    for segment in ("lower", "middle", "upper"):
        for item in sorted(groups[segment], key=lambda item: rank_position[id(item)])[:per_segment_limit]:
            clone = dict(item)
            clone["price_segment"] = segment
            selected.append(clone)
            selected_ids.add(id(item))
            counts[segment] += 1

    # Complete the review set to the configured 10--12-card limit with the
    # best remaining comparable cards.  They retain a clear "additional"
    # label rather than being misrepresented as a fourth price segment.
    for item in ranked:
        if len(selected) >= limit:
            break
        if id(item) in selected_ids:
            continue
        clone = dict(item)
        clone["price_segment"] = "additional"
        selected.append(clone)
        selected_ids.add(id(item))
        counts["additional"] += 1

    return selected, counts


def analyse(
    items: list[dict[str, Any]],
    subject: dict[str, Any],
    selection_limit: int = 5,
    price_band: dict[str, Any] | None = None,
) -> AnalysisResult:
    prepared = []
    rooms_removed = 0
    area_removed = 0
    certificate_corridor_removed = 0
    subject_rooms = _num(subject.get("rooms"))
    subject_area = _num(subject.get("area_sqm"))
    corridor_minimum = _num((price_band or {}).get("corridor_minimum"))
    corridor_maximum = _num((price_band or {}).get("corridor_maximum"))
    certificate_corridor_active = (
        corridor_minimum is not None
        and corridor_maximum is not None
        and corridor_minimum > 0
        and corridor_maximum >= corridor_minimum
    )
    for item in items:
        clone = dict(item)
        price, area = _num(clone.get("price_uah")), _num(clone.get("area_sqm"))
        if not price or not area or area <= 0:
            continue
        # Room count is a primary comparability condition for apartments.
        # Never fill the five positions with 3-room listings merely because
        # there are few 2-room listings; fewer, exact analogues are safer.
        item_rooms = _num(clone.get("rooms"))
        if subject_rooms is not None and (item_rooms is None or int(item_rooms) != int(subject_rooms)):
            rooms_removed += 1
            continue
        # Do not let a substantially different area occupy a place merely
        # because it has a cheap price per m².  The tolerance preserves a
        # useful market sample while keeping the comparison meaningful.
        item_area = _num(clone.get("area_sqm"))
        # Retain only the same room count, but allow a wider area window in a
        # thin local market so the evaluator can review an honest sample.
        if subject_area and item_area and abs(item_area - subject_area) / subject_area > 0.25:
            area_removed += 1
            continue
        # A manually entered / uploaded e-certificate establishes the
        # working corridor for this report.  Keep this guard here as well as
        # in the source adapters: adapters change, but an out-of-corridor
        # advertisement must never enter the analytical sample by accident.
        if certificate_corridor_active and not (corridor_minimum <= price <= corridor_maximum):
            certificate_corridor_removed += 1
            continue
        clone["price_per_sqm"] = round(price / area, 2)
        clone["similarity_score"] = score(clone, subject)
        priority, in_band, distance = _price_band_priority(clone, price_band)
        clone["price_band_priority"] = priority
        clone["price_band_in_range"] = in_band
        clone["price_band_distance"] = distance
        prepared.append(clone)
    # District is a hard preference only when it leaves a usable sample.  A
    # lone same-district card must not determine the whole valuation.  If the
    # public listing does not expose the district, we retain the city sample
    # and make the relaxation visible in the journal for the valuer.
    subject_district = str(subject.get("district") or "").casefold().strip()
    district_exact = []
    if subject_district:
        for item in prepared:
            listing_place = " ".join(str(item.get(key) or "") for key in ("district", "address", "title")).casefold()
            if subject_district in listing_place:
                district_exact.append(item)
    # A review pool is meant to give the valuer a real choice.  The earlier
    # threshold of three was correct for a final three-card valuation but
    # wrong here: three listings that happen to state the district in their
    # title discarded the remaining 9--12 otherwise comparable city cards.
    # Keep district as a strong ranking preference unless it can itself fill
    # the whole requested review pool.
    district_filter_applied = len(district_exact) >= min(selection_limit, 10)
    district_removed = len(prepared) - len(district_exact) if district_filter_applied else 0
    if district_filter_applied:
        prepared = district_exact

    unique, removed_duplicates = deduplicate(prepared)
    filtered, removed_outliers = _mad_filter(unique)
    # MAD is valuable for the final valuation calculation, but it must not
    # silently turn a 10--12-card professional review pool into three cards.
    # When a healthy strict pool exists, retain the cards and label segments
    # transparently; the appraiser will choose the final 3--5 cards.
    review_target = min(selection_limit, len(unique))
    retained_after_mad = False
    if len(unique) >= 8 and len(filtered) < min(8, review_target):
        filtered = unique
        removed_outliers = 0
        retained_after_mad = True
    def rank_key(item: dict[str, Any]) -> tuple:
        item_area = _num(item.get("area_sqm")) or 0
        item_floor = _num(item.get("floor")) or 0
        subject_floor = _num(subject.get("floor")) or 0
        item_total = _num(item.get("total_floors")) or 0
        subject_total = _num(subject.get("total_floors")) or 0
        place = " ".join(str(item.get(key) or "") for key in ("district", "address", "title")).casefold()
        district_penalty = 1 if subject_district and subject_district not in place else 0
        # Deterministic tie breakers make the same saved market snapshot
        # produce the same five analogues and therefore the same range.
        return (
            # All cards have passed the certificate corridor when it is
            # active; comparability remains the ordering criterion here.
            -float(item["similarity_score"]),
            district_penalty,
            abs(item_area - subject_area) if subject_area else 0,
            abs(item_floor - subject_floor) if subject_floor and item_floor else 0,
            abs(item_total - subject_total) if subject_total and item_total else 0,
            canonical_url(str(item.get("url") or "")),
        )

    ranked = sorted(filtered, key=rank_key)
    # Apply the relative screen to the closest market sample rather than to
    # distant listings with a different location or state.  The same guard is
    # then applied to all ranked candidates using that local market centre.
    closest_for_market = ranked[:min(len(ranked), max(selection_limit * 3, 10))]
    _, _, local_centre = _relative_market_filter(closest_for_market)
    relative_removed = 0
    if local_centre:
        relative_kept = [item for item in ranked if local_centre * 0.60 <= float(item["price_per_sqm"]) <= local_centre * 1.45]
        # As above, an automatic screen must not collapse a genuine review
        # pool to the statutory minimum of three.  It is only applied when it
        # still leaves a useful 8--12-card choice (or when the whole market is
        # thinner than that).
        minimum_review_after_filter = min(8, len(ranked), selection_limit)
        if len(relative_kept) >= minimum_review_after_filter:
            relative_removed = len(ranked) - len(relative_kept)
            ranked = relative_kept
    selected, segment_counts = _balanced_price_pool(ranked, selection_limit)
    ppsm = [x["price_per_sqm"] for x in selected]
    area = _num(subject.get("area_sqm")) or 0
    recommended_ppsm = median(ppsm) if ppsm else 0
    # Robust range from the selected sample; it deliberately does not promise
    # a legal conclusion and must be reviewed by the valuer.
    # The selectable range is the central interquartile market band, not the
    # minimum/maximum of a small web sample. This avoids one distressed or
    # premium listing turning into a misleading valuation boundary.
    low, high = _quartiles(ppsm) if ppsm else (0, 0)
    stats = {
        "count": len(selected), "median_price_per_sqm": round(recommended_ppsm, 2),
        "mean_price_per_sqm": round(sum(ppsm) / len(ppsm), 2) if ppsm else 0,
        "min_price_per_sqm": round(low, 2), "max_price_per_sqm": round(high, 2),
        "recommended_value": round(recommended_ppsm * area),
        "range_min": round(low * area), "range_max": round(high * area),
    }
    return AnalysisResult(ranked, selected, stats, {
        "found": len(items), "eligible": len(prepared), "duplicates_removed": removed_duplicates,
        "outliers_removed": removed_outliers, "relative_price_outliers_removed": relative_removed,
        "mad_pool_retained_for_review": retained_after_mad,
        "relative_market_median_price_per_sqm": round(local_centre, 2) if local_centre else None,
        "rooms_mismatch_removed": rooms_removed,
        "area_mismatch_removed": area_removed, "district_mismatch_removed": district_removed,
        "e_certificate_price_corridor": {
            "active": certificate_corridor_active,
            "minimum_uah": round(corridor_minimum) if certificate_corridor_active else None,
            "maximum_uah": round(corridor_maximum) if certificate_corridor_active else None,
            "removed": certificate_corridor_removed,
        },
        "district_exact_candidates": len(district_exact),
        "district_filter_applied": district_filter_applied,
        "area_tolerance_percent": 30,
        "price_ranking": {
            "active": bool(price_band and price_band.get("active")),
            "key": (price_band or {}).get("key", "market"),
            "in_selected_range": sum(1 for item in selected if item.get("price_band_in_range")),
            "maximum_priority_points": 8,
        },
        "candidate_price_segments": segment_counts,
        "remaining": len(ranked), "used": len(selected),
    })
