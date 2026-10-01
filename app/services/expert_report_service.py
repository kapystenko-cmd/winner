"""Evidence-bound enrichment for the optional full expert report.

This service deliberately separates image observations from narrative writing:
Gemini sees uploaded photos/maps, while DeepSeek receives only the resulting
structured observations.  Neither model may invent infrastructure, distances,
repair quality, or a legal conclusion when those facts are not in evidence.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from urllib.parse import quote_plus

import google.generativeai as genai

from app.core.config import settings
from app.services.ocr_service import _parse_model_json


PHOTO_AND_MAP_PROMPT = """You analyse evidence for a Ukrainian real-estate appraisal draft.
The attached files are labelled in the order stated below. Read photographs and,
when supplied, a map image. Return ONLY valid JSON:
{
  "photos_reviewed": 0,
  "visible_features": [],
  "condition_observations": [],
  "location_observations": [],
  "address_or_map_labels_visible": [],
  "needs_appraiser_confirmation": []
}

Rules:
- Describe only facts visibly present: facade, entrance, rooms, doors, windows,
  finishes, furniture, visible utilities, map labels and landmarks.
- Do not identify people, read private personal data, infer a precise address
  from a facade, calculate distances, claim a renovation level, or make a
  value judgement unless the supplied evidence explicitly states it.
- If an image is unclear or a fact is absent, omit that fact entirely. Do not
  mention missing information, a need for verification, or the appraiser.
- Use concise Ukrainian phrases. No markdown and no prose outside JSON.
"""


def _mime(path: Path) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".pdf": "application/pdf",
    }.get(path.suffix.lower(), "image/jpeg")


def _default_evidence(report) -> dict:
    raw = getattr(report, "ocr_raw", None) or {}
    address = str(getattr(report, "address", "") or raw.get("address") or "").strip()
    city = str(raw.get("city") or "").strip()
    query = ", ".join(item for item in (address, city, "Україна") if item)
    return {
        "photos_reviewed": 0,
        "visible_features": [],
        "condition_observations": [],
        "location_observations": [],
        "address_or_map_labels_visible": [],
        "needs_appraiser_confirmation": [],
        "map_attached": bool(getattr(report, "location_map_files", None) or []),
        "map_url": "https://www.openstreetmap.org/search?query=" + quote_plus(query) if query else None,
    }


async def analyse_property_evidence(report) -> dict:
    """Inspect at most eight uploaded photos and one optional map.

    The optional mode must remain bounded: a full package cannot turn a batch
    of 30 photos into 30 remote AI calls.  One multimodal request is used.
    """
    evidence = _default_evidence(report)
    if not settings.gemini_api_key:
        return evidence

    files: list[tuple[str, Path]] = []
    for number, source in enumerate(getattr(report, "object_photo_files", None) or [], start=1):
        path = Path(str(source))
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            files.append((f"ФОТО ОБ'ЄКТА {number}", path))
        if len(files) >= 8:
            break
    for source in getattr(report, "location_map_files", None) or []:
        path = Path(str(source))
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
            files.append(("КАРТА РОЗТАШУВАННЯ", path))
            break
    if not files:
        # Object photographs are optional.  The full package still has a
        # useful document-, analogue- and location-based narrative without
        # them, and must never be blocked by their absence.
        return evidence

    try:
        # Camera originals are often 8–25 MB each. Gemini needs the visible
        # scene, not the original sensor resolution; compact copies make the
        # optional package-three analysis reliable and bounded.
        prepared_files: list[tuple[str, Path]] = []
        with tempfile.TemporaryDirectory(prefix="ocinka-expert-evidence-") as work:
            for index, (label, path) in enumerate(files, start=1):
                if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                    prepared_files.append((label, path))
                    continue
                try:
                    from PIL import Image, ImageOps
                    with Image.open(path) as original:
                        image = ImageOps.exif_transpose(original).convert("RGB")
                        image.thumbnail((1400, 1400))
                        compact = Path(work) / f"evidence-{index}.jpg"
                        image.save(compact, format="JPEG", quality=80, optimize=True)
                        prepared_files.append((label, compact))
                except Exception:
                    prepared_files.append((label, path))

            print(
                f"Expert photo/map analysis started: report={report.id}, files={len(prepared_files)}, "
                f"model={settings.gemini_model}",
                flush=True,
            )
            genai.configure(api_key=settings.gemini_api_key)
            content = [PHOTO_AND_MAP_PROMPT + "\nФайли: " + ", ".join(label for label, _ in prepared_files)]
            for _, path in prepared_files:
                content.append({"mime_type": _mime(path), "data": path.read_bytes()})
            model = genai.GenerativeModel(settings.gemini_model)
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    model.generate_content,
                    content,
                    request_options={"timeout": min(settings.gemini_ocr_timeout_seconds, 60)},
                ),
                timeout=min(settings.gemini_ocr_timeout_seconds + 10, 70),
            )
            extracted = _parse_model_json(response.text)
        if isinstance(extracted, dict):
            for key in ("photos_reviewed", "visible_features", "condition_observations", "location_observations", "address_or_map_labels_visible", "needs_appraiser_confirmation"):
                if key in extracted:
                    evidence[key] = extracted[key]
            photo_count = len([label for label, _ in files if label.startswith("ФОТО")])
            reported_count = int(evidence.get("photos_reviewed") or photo_count)
            evidence["photos_reviewed"] = min(photo_count, reported_count)
            print(
                f"Expert photo/map analysis finished: report={report.id}, photos={evidence['photos_reviewed']}, "
                f"features={len(evidence.get('visible_features') or [])}",
                flush=True,
            )
        else:
            print(f"Expert photo/map analysis returned no JSON: report={report.id}", flush=True)
        return evidence
    except Exception as error:
        print(f"Expert photo/map analysis error: {error}", flush=True)
        # A temporary AI outage must not turn into a sentence inside the
        # report or prevent the ordinary report package from being issued.
        return evidence
