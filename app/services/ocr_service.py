"""Document OCR with Gemini extraction and optional Claude verification."""
import json
import base64
import random
import asyncio
import time
import re
from io import BytesIO
from pathlib import Path
from typing import Optional
import google.generativeai as genai
import anthropic
from app.core.config import settings


def _parse_model_json(text: str) -> dict:
    """Extract one JSON object from an AI response without accepting prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0].strip()
    # Models occasionally prepend a short label despite the instruction.
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]
    return json.loads(text)


def _normalise_page_orientations(value) -> list[dict]:
    """Keep only usable, auditable Gemini orientation hints.

    These values are saved with the OCR extract and later used only as a
    fallback to local readability verification during PDF assembly.  Keeping
    them normalised prevents malformed model output from affecting another
    document or another page.
    """
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    normalised = []
    seen_pages = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("page", 1))
            degrees = int(item.get("rotate_clockwise_degrees", 0)) % 360
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        if page < 1 or page in seen_pages or degrees not in (0, 90, 180, 270):
            continue
        seen_pages.add(page)
        normalised.append({
            "page": page,
            "rotate_clockwise_degrees": degrees,
            "confidence": max(0.0, min(1.0, confidence)),
        })
    return sorted(normalised, key=lambda item: item["page"])

OCR_PROMPT = """You are an OCR specialist for Ukrainian real-estate appraisal documents.
Read the supplied scan or PDF carefully. It may be a technical passport, ownership document, State Register extract, cadastral extract, or a photograph of a document.

Extract ONLY values that are explicitly visible; never infer or invent a value. Return null when the value is absent or unreadable.

Classify the appraisal object before extracting other fields. Return object_type strictly as one of: apartment, house, land, commercial, or null. apartment means a separate flat/unit in a multi-apartment building; house means a detached or attached residential house/estate; land means a land parcel; commercial means a non-residential premises, office, shop, warehouse, production or other commercial property. Do not use a document owner's home address to classify the appraisal object.

Classify document_type strictly as one of: technical passport, ownership certificate, state register extract, cadastral extract, consolidated valuation act, passport, tax identification card, id card, or null if none apply. Base this ONLY on the document's own printed title/header text on the page — never on general visual layout (a numbered grid, a table of figures, or a stamp appears on several different document types and is not by itself distinguishing). Use these header words to decide:
- technical passport: the printed header reads "ТЕХНІЧНИЙ ПАСПОРТ", or the page is a room-by-room "ЕКСПЛІКАЦІЯ" area table, or a "ПОВЕРХОВИЙ ПЛАН" floor sketch — these three page types belong to the same technical passport and must all be labelled technical passport, never consolidated valuation act.
- ownership certificate: the printed header reads "СВІДОЦТВО" (про право власності на нерухоме майно).
- state register extract: the printed header reads "ВИТЯГ" з Державного реєстру речових прав (на нерухоме майно).
- cadastral extract: the printed header reads "ВИТЯГ" з Державного земельного кадастру, or shows a cadastral map/parcel extract.
- consolidated valuation act: the printed header reads "ЗВЕДЕНИЙ АКТ" вартості будівель, господарських будівель та споруд — a monetary valuation table for buildings/structures, distinct from the technical passport's physical room/area table.
- passport / id card: a government-issued personal identity document (photo of the person, passport-style biodata page or ID card) — never a real-estate document.
- tax identification card: "КАРТКА ПЛАТНИКА ПОДАТКІВ" / РНОКПП.
If the header text is not legible enough to be sure, return null rather than guessing from layout alone.

Where to look:
- Technical passport: object type, full address, total/living/kitchen/auxiliary areas, room count, floor, total floors, building year, wall material and object plan. For an apartment or house, area_total is the number next to "загальна площа"; never replace it with living area.
- Ownership document / State Register extract: owner full name, full address, registration number, registration date, property right and cadastral number when present.
- Owner passport or ID card: use only to confirm owner_name against the ownership document. Do NOT extract passport series/number, date of birth, tax number, or other unnecessary personal data.
- Cadastral extract: cadastral number, land_area_sqm or land_area_ha, intended use, ownership and location.

Normalize: numbers only for numeric fields (54.9, not "54,9 кв.м"), city without "м.", owner as full Ukrainian name, address as one complete string. city is a separate required field whenever the appraisal-object address visibly contains a Ukrainian city, town, urban-type settlement or village. Copy the locality from the object address; do not leave city null merely because the street/building part is also present.
Also determine the visual orientation from the direction in which the document's text is readable. Do NOT use page width/height to decide orientation. `rotate_clockwise_degrees` means the clockwise rotation required to make the text upright; use only 0, 90, 180 or 270. `page_orientations` is mandatory: for a photo return exactly one item; for a PDF return one item for every visible source page, including pages that already have upright text (rotation 0). If text cannot be read, set confidence below 0.50 and do not guess.

CRITICAL ORIENTATION RULES:
- Look at the LARGEST block of readable text on the page (title, body text — not stamps or watermarks).
- If the text reads normally left-to-right, top-to-bottom → rotate_clockwise_degrees = 0.
- If the text is sideways and you need to tilt your head LEFT to read it → rotate_clockwise_degrees = 90.
- If the text is completely upside down → rotate_clockwise_degrees = 180.
- If the text is sideways and you need to tilt your head RIGHT to read it → rotate_clockwise_degrees = 270.
- Set confidence >= 0.85 when you can clearly read the text direction. Only set confidence below 0.50 when you genuinely cannot determine orientation (blank page, only stamps, no readable text).
- A scanned document photographed at an angle is NOT a rotation — it is still 0 if the text baseline is approximately horizontal.

Return ONLY valid JSON with exactly these fields:
document_type, object_type, address, city, region, district, neighborhood, street, building, apartment,
area_total, area_living, area_kitchen, area_auxiliary, land_area_sqm, land_area_ha,
rooms, floor, total_floors, year_built, cadastral_number, owner_name,
registration_number, registration_date, wall_material, condition,
missing_documents, missing_data, calculated_fields, field_sources, page_orientations, confidence.
field_sources is an object such as {"area_total":"technical passport, page 2"}.
calculated_fields is an object such as {"area_total":{"value":54.9,"formula":"27.5 living + 9.0 kitchen + 18.4 auxiliary","sources":["technical passport, page 2"]}}. Use it only when every term is explicitly visible in the documents or when the technical passport itself gives the formula.
page_orientations is an array such as [{"page":1,"rotate_clockwise_degrees":90,"confidence":0.96}]. It is for presentation of the original scan in the report only; it must be based on the readable orientation of text.
missing_documents is an array of Ukrainian document types absent from the uploaded set.
missing_data is an array of Ukrainian field names that cannot be found after reviewing the complete uploaded set. Required for calculation:
- apartment/house/commercial: object_type, address, area_total; rooms and floor should be listed if absent for an apartment;
- land: object_type, address or location, land area, cadastral_number;
- final conclusion: owner_name and a legal source document.
confidence is 0 to 1. No markdown and no explanation."""


def _ocr_payload(file_path: str) -> tuple[bytes, str]:
    """Shrink phone photos before sending them to Gemini.

    A 12–48 MP camera original makes the vision request slow but does not
    improve recognition of an A4 document.  A 2200px long side preserves text
    comfortably while reducing transfer time and provider processing load.
    PDFs are kept intact because they may contain multiple source pages.
    """
    source = Path(file_path)
    original = source.read_bytes()
    ext = source.suffix.lower()
    mime_map = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }
    mime = mime_map.get(ext, "image/jpeg")
    if ext not in {".jpg", ".jpeg", ".png"} or len(original) <= 1_500_000:
        return original, mime
    try:
        from PIL import Image, ImageOps
        with Image.open(BytesIO(original)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
            payload = output.getvalue()
        print(
            f"OCR image compressed: file={source.name}, "
            f"bytes={len(original)}->{len(payload)}",
            flush=True,
        )
        return payload, "image/jpeg"
    except Exception as error:
        print(f"OCR image compression skipped: file={source.name}, error={error}", flush=True)
        return original, mime


async def ocr_gemini(file_path):
    """OCR through the configured Gemini model."""
    if not settings.gemini_api_key:
        return None
    try:
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(settings.gemini_model)
        file_bytes, mime = _ocr_payload(file_path)
        # The legacy Gemini client is synchronous.  Run it in a worker thread
        # so several independent document pages can be analysed concurrently.
        response = await asyncio.wait_for(
            asyncio.to_thread(
                model.generate_content,
                [OCR_PROMPT, {"mime_type": mime, "data": file_bytes}],
                request_options={"timeout": settings.gemini_ocr_timeout_seconds},
            ),
            timeout=settings.gemini_ocr_timeout_seconds + 10,
        )
        data = _parse_model_json(response.text)
        # Persist a page-specific orientation hint with the same OCR extract.
        # It is not merged into another file and is rechecked locally when the
        # final PDF is made.
        data["page_orientations"] = _normalise_page_orientations(
            data.get("page_orientations")
        )
        data["_provider"] = "gemini"
        return data
    except Exception as e:
        print("Gemini OCR error: " + str(e))
        return None


async def ocr_claude(file_path):
    """Fallback OCR through the configured Claude model."""
    if not settings.anthropic_api_key:
        return None
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        file_bytes = Path(file_path).read_bytes()
        ext = Path(file_path).suffix.lower()
        mime_map = {
            ".pdf": "application/pdf",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
        }
        mime = mime_map.get(ext, "image/jpeg")
        b64 = base64.standard_b64encode(file_bytes).decode()
        content = [{"type": "text", "text": OCR_PROMPT}]
        if mime == "application/pdf":
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
        else:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.messages.create,
                model=settings.claude_ocr_model,
                max_tokens=2000,
                messages=[{"role": "user", "content": content}],
            ),
            timeout=settings.claude_verify_timeout_seconds,
        )
        data = _parse_model_json(response.content[0].text)
        data["page_orientations"] = _normalise_page_orientations(
            data.get("page_orientations")
        )
        data["_provider"] = "claude"
        return data
    except Exception as e:
        print("Claude OCR error: " + str(e))
        return None


async def process_document(file_path):
    """Gemini -> Claude -> error"""
    result = await ocr_gemini(file_path)
    if result and result.get("confidence", 0) >= 0.7:
        return result
    result_claude = await ocr_claude(file_path)
    if result_claude and result_claude.get("confidence", 0) >= 0.5:
        return result_claude
    if result:
        result["_needs_review"] = True
        return result
    if result_claude:
        result_claude["_needs_review"] = True
        return result_claude
    return {
        "_provider": "none",
        "_needs_review": True,
        "confidence": 0,
    }


VERIFY_PROMPT = """You are a quality-control reviewer for Ukrainian real-estate appraisal documents.
Compare every attached source document and the preliminary JSON extracts supplied below. Each extract contains _file_name so you can identify its source.
Return ONLY valid JSON in this exact form:
{
  "resolved": {"object_type": null, "address": null, "city": null, "region": null, "district": null, "neighborhood": null, "street": null, "building": null, "apartment": null, "area_total": null, "area_living": null, "area_kitchen": null, "area_auxiliary": null, "land_area_sqm": null, "land_area_ha": null, "rooms": null, "floor": null, "total_floors": null, "year_built": null, "cadastral_number": null, "owner_name": null, "registration_number": null, "registration_date": null, "wall_material": null, "condition": null, "documents_found": [], "missing_documents": [], "missing_data": [], "calculated_fields": {}, "field_sources": {}},
  "confidence": 0.0,
  "needs_review": false,
  "conflicts": [
    {"field": "area_total", "values_by_document": [{"file": "tech_passport.pdf", "value": "54.9"}, {"file": "registry.pdf", "value": "55.1"}], "note": "Коротке пояснення українською"}
  ]
}
Rules:
- Never invent a value; preserve null when a value is absent.
- Resolve object_type strictly as apartment, house, land, commercial, or null. Determine it from the appraisal object in the technical, ownership, or cadastral document, never from the owner's personal address. A separate flat/unit is apartment; a residential estate or detached/attached building is house; a cadastral land parcel is land; a non-residential premises, office, shop, warehouse, production or other commercial property is commercial.
- Source priority: the technical passport / technical inventory is the source for factual technical characteristics: area_total, area_living, area_kitchen, area_auxiliary, rooms, floor, total_floors, wall_material. An ownership document or State Register extract establishes the current owner, rights, registration number and the current legal address; it is NOT a replacement for the technical passport when choosing physical area. A cadastral extract is the source for cadastral_number and land data.
- Never confuse general area with living, kitchen or auxiliary area. Use only a value explicitly labelled "загальна площа" for area_total. If the technical passport gives a different general area from a legal document, keep the technical-passport value in resolved.area_total, create a conflict, and explain that the appraiser must check whether the technical passport is current. Do not silently add or substitute living area.
- If equally authoritative documents differ, add a conflict and set needs_review=true.
- If a field appears in only one relevant document, it is valid; do not call it a conflict merely because other documents omit it.
- Build documents_found from the entire uploaded set, with filename and recognized document type. For apartment/house require a technical passport and an ownership document or State Register extract. For land require a cadastral extract and an ownership document or State Register extract. Object photographs are required for the final appraisal package but are not a replacement for source documents.
- Never list a cadastral extract as missing for an apartment or house.  It is required only for a land parcel.  Do not list object photographs as a missing source document: they are uploaded separately in the workspace before report generation.
- Put absent document types in resolved.missing_documents. Put absent individual fields in resolved.missing_data. These are different lists.
- Flag every material inconsistency: address, owner, area, rooms, floor, total_floors, cadastral_number, registration_number, year_built.
- Do not let preliminary JSON override what is visibly written in the documents.
- You may calculate a missing area ONLY when all terms of an explicit technical-passport formula are visible. Example: total area = living area + kitchen + auxiliary area. Put the value, formula and document/page evidence in resolved.calculated_fields and use the same value in resolved.area_total. If any term is absent, do not estimate: list the field in resolved.missing_data and set needs_review=true.
- For owner passport/ID, use only the full name as corroboration; never include passport number, date of birth, tax number or other unnecessary personal data.
Answer in Ukrainian JSON only. Keep every string on one line; escape quotation marks inside values. Do not add any text before or after the JSON."""


def _claude_document_block(file_path: str) -> dict:
    data = base64.standard_b64encode(Path(file_path).read_bytes()).decode()
    ext = Path(file_path).suffix.lower()
    mime = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(ext, "image/jpeg")
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": mime, "data": data}}
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}


async def verify_documents_with_claude(file_paths: list[str], preliminary: list[dict]) -> dict | None:
    """Cross-check all uploaded documents; return conflicts for appraiser review."""
    if not settings.anthropic_api_key or not settings.claude_ocr_verify_enabled:
        return None
    try:
        content = [
            {"type": "text", "text": VERIFY_PROMPT},
            {"type": "text", "text": "PRELIMINARY EXTRACTS:\n" + json.dumps(preliminary, ensure_ascii=False)},
        ]
        # Four primary sources are sufficient for the supported object workflow
        # and prevent an accidental oversized API request.
        content.extend(_claude_document_block(path) for path in file_paths[:4])
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.messages.create,
                model=settings.claude_ocr_model,
                # A full cross-check for several scans needs more room than a
                # single-document OCR response.  Cutting it off produces invalid
                # JSON and used to block the analogue-search workflow.
                max_tokens=5000,
                messages=[{"role": "user", "content": content}],
            ),
            timeout=settings.claude_verify_timeout_seconds,
        )
        return _parse_model_json(response.content[0].text)
    except Exception as error:
        # Verification is a safety layer: preserve the Gemini result and
        # surface the issue in logs rather than blocking document storage.
        print("Claude OCR verification error: " + str(error))
        return None


_EXTRACT_FIELDS = (
    "object_type", "address", "city", "region", "district", "neighborhood", "street", "building", "apartment",
    "area_total", "area_living", "area_kitchen", "area_auxiliary", "land_area_sqm", "land_area_ha",
    "rooms", "floor", "total_floors", "year_built", "cadastral_number", "owner_name",
    "registration_number", "registration_date", "wall_material", "condition",
)

_TECHNICAL_FIELDS = {
    "object_type", "area_total", "area_living", "area_kitchen", "area_auxiliary", "rooms", "floor",
    "total_floors", "year_built", "wall_material", "condition",
}
_LEGAL_FIELDS = {"address", "city", "region", "street", "building", "apartment", "owner_name", "registration_number", "registration_date"}
_CADASTRAL_FIELDS = {"cadastral_number", "land_area_sqm", "land_area_ha"}


def _has_value(value) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _document_kind(extract: dict) -> str:
    """Return searchable document type text without trusting a filename alone."""
    return " ".join(str(extract.get(key, "")) for key in ("document_type", "_file_name")).lower()


def _source_priority(field: str, extract: dict) -> int:
    """Select the legally relevant source when Claude cross-checking is disabled."""
    kind = _document_kind(extract)
    technical = any(word in kind for word in ("техніч", "техпаспорт", "technical", "inventory"))
    legal = any(word in kind for word in ("витяг", "реєстр", "право", "догов", "ownership", "registry", "title"))
    cadastral = any(word in kind for word in ("кадастр", "cadastral"))
    priority = 0
    if field in _TECHNICAL_FIELDS:
        priority += 100 if technical else 10
    elif field in _LEGAL_FIELDS:
        priority += 100 if legal else 10
    elif field in _CADASTRAL_FIELDS:
        priority += 100 if cadastral else 10
    try:
        priority += int(float(extract.get("confidence", 0)) * 10)
    except (TypeError, ValueError):
        pass
    return priority


def _city_from_address(address) -> str | None:
    """Extract an explicitly labelled Ukrainian locality from an object address."""
    if not isinstance(address, str):
        return None
    match = re.search(
        r"(?:^|[,;]\s*)(?:м\.?|місто|г\.?|смт\.?|селище|с\.\s*|село)\s*([^,;]+)",
        address,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    city = re.sub(r"\s+", " ", match.group(1)).strip(" .")
    return city or None


def _region_from_address(address) -> str | None:
    """Extract oblast name from Ukrainian address."""
    if not isinstance(address, str):
        return None
    match = re.search(
        r"([\u0410-\u042F\u0406\u0407\u0490\u0404\u0430-\u044F\u0456\u0457\u0491\u0454\'\'-]+\u0441\u044C\u043A\u0430)\s+(?:\u043E\u0431\u043B\u0430\u0441\u0442\u044C|\u043E\u0431\u043B\\.?)",
        address, flags=re.IGNORECASE,
    )
    if not match:
        return None
    region = match.group(1).strip()
    return region[0].upper() + region[1:].lower() if region else None


def infer_region_from_extracts(extracts: list[dict]) -> str | None:
    """Find region across all documents."""
    explicit = [item for item in extracts if isinstance(item.get("region"), str) and item["region"].strip()]
    if explicit:
        return max(enumerate(explicit), key=lambda item: (_source_priority("region", item[1]), -item[0]))[1]["region"].strip()
    from_address = [(item, _region_from_address(item.get("address"))) for item in extracts]
    from_address = [(item, region) for item, region in from_address if region]
    if from_address:
        return max(enumerate(from_address), key=lambda item: (_source_priority("region", item[1][0]), -item[0]))[1][1]
    return None


def infer_city_from_extracts(extracts: list[dict]) -> str | None:
    """Find city in every document, not merely the first uploaded page."""
    explicit = [item for item in extracts if isinstance(item.get("city"), str) and item["city"].strip()]
    if explicit:
        return max(enumerate(explicit), key=lambda item: (_source_priority("city", item[1]), -item[0]))[1]["city"].strip()
    from_address = [(item, _city_from_address(item.get("address"))) for item in extracts]
    from_address = [(item, city) for item, city in from_address if city]
    if from_address:
        return max(enumerate(from_address), key=lambda item: (_source_priority("city", item[1][0]), -item[0]))[1][1]
    return None


def merge_document_extracts(extracts: list[dict]) -> dict:
    """Merge every Gemini page into one traceable appraisal-data draft.

    Gemini is asked about one file at a time for speed and reliability.  The
    report must nevertheless use the whole set: a passport page normally has
    no object address, while the technical passport has the physical areas.
    """
    result: dict = {"_provider": "gemini_merged", "field_sources": {}, "calculated_fields": {}}
    for field in _EXTRACT_FIELDS:
        candidates = [item for item in extracts if _has_value(item.get(field))]
        if not candidates:
            result[field] = None
            continue
        chosen = max(enumerate(candidates), key=lambda item: (_source_priority(field, item[1]), -item[0]))[1]
        result[field] = chosen[field]
        source = chosen.get("field_sources", {}).get(field) if isinstance(chosen.get("field_sources"), dict) else None
        result["field_sources"][field] = source or chosen.get("_file_name", "uploaded document")

    # City may be explicit only on a later ownership/registry page.
    result["city"] = infer_city_from_extracts(extracts) or result.get("city")
    if result.get("city") and "city" not in result["field_sources"]:
        result["field_sources"]["city"] = "uploaded document"

    result["region"] = infer_region_from_extracts(extracts) or result.get("region")
    if result.get("region") and "region" not in result["field_sources"]:
        result["field_sources"]["region"] = "uploaded document"

    result["documents_found"] = [
        {"file": item.get("_file_name"), "document_type": item.get("document_type")}
        for item in extracts
    ]
    result["_document_extracts"] = [dict(item) for item in extracts]
    result["_conflicts"] = []
    result["_needs_review"] = False
    try:
        result["confidence"] = max(float(item.get("confidence", 0)) for item in extracts)
    except (TypeError, ValueError):
        result["confidence"] = 0
    # Per-page "missing" lists cannot describe the whole upload; calculate
    # only genuinely unresolved core data after the merge.
    required = ["object_type", "address"]
    if result.get("object_type") in {"land", "land_plot", "земельна ділянка"}:
        required.extend(["cadastral_number", "land_area_sqm"])
    else:
        required.append("area_total")
        # For an apartment, room count is a primary comparable criterion.
        # A market search must not silently substitute 3-/4-room listings
        # when OCR could not read this value from the technical passport.
        if str(result.get("object_type") or "").strip().lower() in {"apartment", "квартира"}:
            required.append("rooms")
    result["missing_data"] = [field for field in required if not _has_value(result.get(field))]
    result["missing_documents"] = []
    return result


def _requires_claude_cross_check(merged: dict, extracts: list[dict]) -> bool:
    """Use the slower cross-document reviewer only when it adds protection.

    Gemini still reads every uploaded page.  Claude is reserved for an
    unresolved core field, a weak page extraction, or a concrete technical
    contradiction.  This preserves the careful workflow without making every
    ordinary, internally consistent report wait for a second vision pass.
    """
    if merged.get("missing_data"):
        return True
    if any(item.get("_needs_review") or item.get("_provider") == "none" for item in extracts):
        return True

    numeric_fields = ("area_total", "rooms", "floor", "total_floors", "year_built")
    for field in numeric_fields:
        values = set()
        for item in extracts:
            value = item.get(field)
            if not _has_value(value):
                continue
            try:
                values.add(round(float(str(value).replace(",", ".")), 2))
            except (TypeError, ValueError):
                values.add(str(value).strip().casefold())
        if len(values) > 1:
            print(f"Claude verification requested: conflicting {field} values={list(values)}", flush=True)
            return True

    object_types = {
        str(item.get("object_type") or "").strip().casefold()
        for item in extracts
        if item.get("object_type")
    }
    if len(object_types) > 1:
        return True

    # Document-type classification is layout-driven and occasionally wrong
    # (a "Зведений акт" table misread as a "Технічний паспорт" was seen on a
    # real report). Types other than "technical passport" — which legitimately
    # spans two pages (area table + floor plan) — should normally appear on
    # exactly one uploaded file each. Two files sharing one of those types
    # most likely means at least one was misclassified.
    single_expected_types = ("ownership certificate", "state register extract", "cadastral extract", "consolidated valuation act")
    type_counts: dict[str, int] = {}
    for item in extracts:
        doc_type = str(item.get("document_type") or "").strip().casefold()
        if doc_type in single_expected_types:
            type_counts[doc_type] = type_counts.get(doc_type, 0) + 1
    if any(count > 1 for count in type_counts.values()):
        print(f"Claude verification requested: duplicate document_type counts={type_counts}", flush=True)
        return True
    return False


async def process_documents(file_paths: list[str]) -> dict:
    """Extract every source and request a cross-check only when it is needed."""
    started = time.perf_counter()
    # Six parallel, reduced-size vision calls keep ordinary sets of documents
    # inside one interactive minute without sending scraper work to this step.
    limit = asyncio.Semaphore(settings.ocr_concurrency)

    async def extract_one(path: str):
        async with limit:
            file_name = Path(path).name
            file_started = time.perf_counter()
            print(f"OCR started: {file_name}", flush=True)
            result = await ocr_gemini(path)
            if result:
                result["_file_name"] = file_name
                print(f"OCR finished: {file_name} in {time.perf_counter() - file_started:.1f}s", flush=True)
            else:
                print(f"OCR unavailable: {file_name} after {time.perf_counter() - file_started:.1f}s", flush=True)
            return result

    # Keep deployments compatible with older config.py versions.  The overall
    # deadline prevents a stalled provider from holding the HTTP request open.
    total_timeout_seconds = getattr(settings, "ocr_total_timeout_seconds", 50)
    tasks = [asyncio.create_task(extract_one(path)) for path in file_paths]
    done, pending = await asyncio.wait(
        tasks,
        timeout=total_timeout_seconds,
    )
    if pending:
        print(
            f"OCR overall deadline reached: completed={len(done)}/{len(tasks)}, "
            f"deadline={total_timeout_seconds}s",
            flush=True,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    results = []
    for task in done:
        try:
            results.append(task.result())
        except Exception as error:
            print(f"OCR task stopped: {error}", flush=True)
    extracts = [item for item in results if item]
    print(f"OCR extracted {len(extracts)}/{len(file_paths)} files in {time.perf_counter() - started:.1f}s", flush=True)
    if not extracts:
        return {"_provider": "none", "_needs_review": True, "confidence": 0}

    result = merge_document_extracts(extracts)
    remaining_seconds = total_timeout_seconds - (time.perf_counter() - started)
    if _requires_claude_cross_check(result, extracts) and remaining_seconds >= 5:
        verification_started = time.perf_counter()
        print("Claude verification started: unresolved or conflicting document data", flush=True)
        try:
            verified = await asyncio.wait_for(
                verify_documents_with_claude(file_paths, extracts),
                timeout=min(settings.claude_verify_timeout_seconds, remaining_seconds),
            )
        except asyncio.TimeoutError:
            verified = None
            print("Claude verification skipped: OCR overall deadline reached", flush=True)
        if verified and isinstance(verified.get("resolved"), dict):
            result = verified["resolved"]
            result["confidence"] = verified.get("confidence", result.get("confidence", 0))
            result["_provider"] = "gemini+claude_verify"
            result["_document_extracts"] = extracts
            result["_conflicts"] = verified.get("conflicts", [])
            result["_needs_review"] = bool(verified.get("needs_review")) or bool(result["_conflicts"])
            print(
                f"Claude verification finished in {time.perf_counter() - verification_started:.1f}s; "
                f"OCR pipeline completed in {time.perf_counter() - started:.1f}s",
                flush=True,
            )
            return result
        # Verification was judged necessary (see _requires_claude_cross_check)
        # but did not come back with a usable result -- disabled/missing key,
        # a timeout, a network error, or a response that failed to parse as
        # the expected JSON shape. This used to set _needs_review=True
        # unconditionally, which shows the appraiser the exact same "знайдено
        # розбіжності" warning as a genuine Claude-confirmed conflict, even
        # though nothing was actually confirmed -- an appraiser has no way to
        # tell "Claude found a real problem" apart from "Claude could not be
        # reached this time" from that message alone. Per the product owner:
        # only a real, Claude-confirmed conflict should raise that warning.
        # A verification that could not run at all is not a conflict, so this
        # now falls back to the same plain Gemini merge used when no
        # cross-check was needed in the first place (result["_needs_review"]
        # is already False from merge_document_extracts, and is left as-is
        # here). _verification_unavailable stays available on the record for
        # logging/audit, without surfacing a false alarm to the appraiser.
        result["_verification_unavailable"] = True
        result["_conflicts"] = []
        print(
            f"Claude verification unavailable or incomplete in {time.perf_counter() - verification_started:.1f}s; "
            f"OCR pipeline continuing on the Gemini merge without a confirmed conflict, "
            f"completed in {time.perf_counter() - started:.1f}s",
            flush=True,
        )
        return result

    print("Claude verification skipped: Gemini extracted a complete consistent set", flush=True)
    result["_verification_skipped"] = True
    # The deterministic merge keeps the source of every extracted field so an
    # appraiser can still audit the fast-path result.
    result["_needs_review"] = False
    print(f"OCR pipeline completed in {time.perf_counter() - started:.1f}s", flush=True)
    return result
