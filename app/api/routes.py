"""API ендпоінти: авторизація, звіти, адмін-панель"""
import os
import io
import uuid
import shutil
import asyncio
import tarfile
import tempfile
import subprocess
import traceback
import re
import logging
from time import perf_counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, EmailStr

from app.core.database import get_db
from app.core.auth import hash_password, verify_password, create_token, get_current_user, get_admin_user
from app.core.config import settings
from app.models.models import (
    User, Report, Analog, AgencyReferral, ActivityLog,
    ReportStatus, EvalMode, DealType, ObjectType, SubscriptionPlan, ValueChangeLog, ValueSelectionMode
)
from app.services.ocr_service import process_documents, process_document, infer_city_from_extracts

logger = logging.getLogger(__name__)
from app.services.expert_report_service import analyse_property_evidence
from app.services.deepseek_service import generate_expert_report_text
from app.services.dimria_service import search_analogs, take_screenshot
from app.services.olx_service import _take_screenshot as take_olx_screenshot
from app.services.report_generator import (
    generate_word_compact,
    generate_word_conclusion,
    generate_full_word_package,
    generate_pdf_conclusion,
    generate_full_pdf_compact,
)


# ========== SCHEMAS ==========

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone: Optional[str] = None
    cert_number: Optional[str] = None
    sod_name: Optional[str] = None
    sod_edrpou: Optional[str] = None
    sod_cert_number: Optional[str] = None
    sod_address: Optional[str] = None
    sod_header_offset_mm: Optional[int] = 0
    newsletter_consent: bool = False

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ProfileUpdateRequest(BaseModel):
    """Editable evaluator and SOD details used as report defaults."""
    full_name: str
    phone: Optional[str] = None
    cert_number: Optional[str] = None
    sod_name: Optional[str] = None
    sod_edrpou: Optional[str] = None
    sod_cert_number: Optional[str] = None
    sod_address: Optional[str] = None
    sod_header_offset_mm: Optional[int] = 0


PROFILE_DOCUMENT_KINDS = {
    "appraiser_certificate": "Кваліфікаційне свідоцтво оцінювача",
    "appraiser_certificate_real_estate": "Кваліфікаційне свідоцтво — нерухомість (ФДМУ)",
    "appraiser_certificate_land": "Кваліфікаційне свідоцтво — земельні ділянки (Держкомзем)",
    "continuing_education": "Підвищення кваліфікації оцінювача",
    "sod_certificate": "Сертифікат СОД / ФДМУ",
    "sod_statutory": "Виписка ЄДР / установчі документи СОД",
    "sod_logo": "Логотип СОД для титулу",
    "other": "Інші реквізити СОД",
}


def _profile_document_public(item: dict) -> dict:
    """Return metadata without exposing a server filesystem path."""
    return {
        "id": item.get("id"),
        "name": item.get("name", "Документ"),
        "kind": item.get("kind", "other"),
        "kind_label": PROFILE_DOCUMENT_KINDS.get(item.get("kind"), PROFILE_DOCUMENT_KINDS["other"]),
        "uploaded_at": item.get("uploaded_at"),
    }

class CreateReportRequest(BaseModel):
    object_type: str = "apartment"
    deal_type: str = "cash"
    eval_mode: str = "standard"

class AdjustValueRequest(BaseModel):
    adjustment_percent: float  # -25 to +25

class SelectValueRequest(BaseModel):
    value: float
    mode: str = "professional"  # automatic / professional / expert
    reason: Optional[str] = None


class SelectAnalogsRequest(BaseModel):
    """Final professional selection from the neutral comparable pool."""
    analog_ids: list[str]


async def _capture_selected_analog_screenshots(report: Report, analogs: list[Analog]) -> int:
    """Capture evidence only after the valuer has selected final cards.

    Candidate selection must be quick.  Browser work is therefore deferred to
    the final report generation request, when screenshots are actually needed
    for the selected package.
    """
    options = report.report_options or {}
    # Diagnostics deliberately use print, not logger.
    #
    # logger.info output does not reach journalctl in this deployment -- a
    # grep for "stage=" over a full hour of real traffic returned nothing,
    # while print lines from the same run were all present. Screenshot
    # failures were therefore invisible: the stage looked skipped when it may
    # simply have been unreported. Until logging is wired up, anything needed
    # for diagnosing this path is printed.
    print(f"Screenshots: entering capture stage, report={report.id}, analogs={len(analogs)}")
    if not _screenshots_enabled(options):
        print(f"Screenshots: disabled for report={report.id}, options={dict(options)}")
        return 0
    screenshot_dir = os.path.join(settings.screenshots_dir, str(report.id))

    async def capture(candidate: Analog) -> bool:
        if not candidate.url:
            # Previously a silent return. With every analog lacking a URL the
            # whole stage produced no output at all, which is indistinguishable
            # from the stage never running.
            print(f"Screenshots: analog {candidate.id} has no URL, skipped")
            return False
        path = os.path.join(screenshot_dir, f"analog_{candidate.rank}.png")
        # Reuse an already-captured screenshot instead of paying for it again.
        # Generation runs inside the HTTP request, so a dropped tab / crashed
        # client / retry re-enters this stage — and each capture is a paid
        # ZenRows call. If this analog's file already exists from an earlier
        # (interrupted) run, reuse it: the evidence is identical and the user's
        # provider quota is not spent twice. A tiny/corrupt leftover (<2 KB) is
        # ignored and re-captured.
        try:
            if os.path.exists(path) and os.path.getsize(path) > 2048:
                candidate.screenshot_path = path
                candidate.screenshot_verified = True
                print(f"Screenshots: reusing existing capture for analog rank={candidate.rank}: {path}")
                return True
        except OSError:
            pass
        screenshotter = take_olx_screenshot if str(candidate.source).casefold() == "olx" else take_screenshot
        try:
            ok = await screenshotter(candidate.url, path)
        except Exception as error:
            print(f"Final analogue screenshot failed: report={report.id}, url={candidate.url}, error={error}")
            return False
        if ok:
            candidate.screenshot_path = path
            candidate.screenshot_verified = True
        return bool(ok)

    # Sequential capture, one analog at a time.
    #
    # Running five Chromium instances concurrently (the previous
    # asyncio.gather) made every capture race for CPU and memory on the
    # VPS. Pages reached page.screenshot() at different stages of loading,
    # so the rendered content height varied between analogs — and the
    # fixed-percentage crop applied later in report_generator then cut
    # correctly for some screenshots and badly for others ("part perfect,
    # part cut off"). One browser at a time gives every page the same,
    # unshared wait budget, so the layout it renders is consistent and the
    # crop percentages apply to the same thing each time.
    #
    # Total wall-clock cost is similar: the concurrent version was not
    # actually five times faster, because the contention it created was
    # the bottleneck.
    for candidate in analogs:
        print(f"Screenshots: will capture analog rank={candidate.rank} "
              f"source={candidate.source} url={candidate.url}")
    # Sequential capture, one analog at a time, with a short gap between.
    #
    # Concurrent gather() fired all four captures in the same second, and
    # ZenRows rejected the extras with AUTH006 "concurrency limit reached":
    # a real report captured 1 of 4, the other three died on that error while
    # the first succeeded. The plan simply does not allow that many parallel
    # ZenRows requests. One at a time stays under the limit.
    #
    # This is NOT the earlier sequential change that broke screenshots — that
    # one added an asyncio.wait_for deadline that cancelled captures mid-flight.
    # Here there is no deadline, just serialisation and a small pause, which is
    # exactly what the search phase already does for the same reason.
    results = []
    for candidate in analogs:
        results.append(await capture(candidate))
        # 2.5s pause between captures. Each ZenRows screenshot takes
        # ~15-30 s itself (fullpage rendering), but sometimes fails
        # fast with HTTP 400/429 — the pause gives the provider room
        # to breathe between failed-fast retries and prevents the
        # concurrency gate from firing all captures in the same second.
        await asyncio.sleep(2.5)
    created = sum(bool(value) for value in results)
    journal = dict(report.search_journal or {})
    journal["final_screenshots"] = {
        "requested": True,
        "created": created,
        "attempted": len(analogs),
        "created_at": datetime.utcnow().isoformat(),
    }
    report.search_journal = journal
    return created

class ReportOptionsRequest(BaseModel):
    # Presentation and evidence level selected by the evaluator.  The legacy
    # boolean fields remain accepted so existing saved drafts still work.
    report_package: Optional[str] = None  # basic / evidence / expert
    include_screenshots: bool = False
    include_olx: bool = True
    include_appraiser_documents: bool = True
    # This is an asking-price negotiation adjustment, not a statutory fixed
    # percentage.  It is applied transparently and only with a written reason.
    trade_adjustment_percent: float = 0
    trade_adjustment_reason: Optional[str] = None
    # The amount is entered by the appraiser.  When the scan is unavailable,
    # explicit confirmation is required and the report must disclose that.
    e_certificate_value: Optional[float] = None
    e_certificate_manual_confirmed: bool = False
    # The range is only a transparent ranking preference for otherwise
    # comparable listings; it must not prescribe a valuation result.
    search_price_band: str = "market"  # lower / center / upper / market
    # Optional long-form package. It is intentionally explicit because it
    # reads uploaded object photos and calls the configured AI services.
    full_expert_mode: bool = False
    # Date selected by the appraiser for valuation and inspection.
    valuation_date: Optional[str] = None
    # Optional evaluator-entered locality. It is used only if the scanner did
    # not extract a city cleanly, and stays in the report audit data.
    manual_city: Optional[str] = None
    format: str = "table"  # table / table_with_screenshots / eoselia


class ConfirmOcrRequest(BaseModel):
    """Explicit appraiser confirmation after a multi-document OCR conflict."""
    note: Optional[str] = None


class PartnerAccountingRequest(BaseModel):
    status: Optional[str] = None  # pending / approved / rejected
    is_partner_active: Optional[bool] = None
    partner_percent: Optional[float] = None
    partner_accrued_uah: Optional[float] = None
    partner_paid_uah: Optional[float] = None


class PartnerApplicationRequest(BaseModel):
    agency_name: str
    contact_person: str
    contact_email: EmailStr
    contact_phone: str
    reports_per_month: Optional[int] = None
    team_size: Optional[int] = None
    message: Optional[str] = None


# ========== ROUTERS ==========
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
reports_router = APIRouter(prefix="/api/reports", tags=["reports"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])
partners_router = APIRouter(prefix="/api/partners", tags=["partners"])


def _as_float(value, default: float = 0.0) -> float:
    """Convert marketplace values without losing thousand separators."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        # OLX may return values such as "380 358 грн" or "52,7".
        raw = str(value).strip().replace("\u00a0", " ").replace("'", "").replace("’", "")
        raw = re.sub(r"[^0-9,.\-\s]", "", raw)
        raw = re.sub(r"\s+", "", raw)
        if raw in {"", ".", ",", "-"}:
            return default
        sign = -1 if raw.startswith("-") else 1
        raw = raw.lstrip("-")
        separators = [symbol for symbol in (",", ".") if symbol in raw]
        if len(separators) == 2:
            decimal_at = max(raw.rfind(","), raw.rfind("."))
            tail = raw[decimal_at + 1:]
            head = re.sub(r"[,.]", "", raw[:decimal_at])
            normalized = f"{head}.{tail}" if len(tail) <= 2 else f"{head}{tail}"
        elif separators:
            separator = separators[0]
            parts = raw.split(separator)
            normalized = "".join(parts) if (len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3)) else ".".join(parts)
        else:
            normalized = raw
        return sign * float(normalized)
    except (TypeError, ValueError):
        return default


def _screenshots_enabled(options: dict | None) -> bool:
    """Package 3 always includes screenshots, including older saved drafts."""
    options = options or {}
    return bool(options.get("include_screenshots")) or str(options.get("report_package") or "").casefold() == "expert"


def _expert_enabled(options: dict | None) -> bool:
    options = options or {}
    return bool(options.get("full_expert_mode")) or str(options.get("report_package") or "").casefold() == "expert"


def _search_price_band(e_certificate_value: float, selection: str = "market") -> dict:
    """Build the single e-certificate corridor used for candidate search.

    The appraiser selects 3--5 candidates first.  Lower/middle/upper is not a
    search preference: every candidate is checked against the complete legal
    75--125% corridor and is then ranked by property similarity.
    """
    # A confirmed e-certificate activates the complete 75–125% working
    # corridor even when the appraiser has not chosen one of its three
    # presentation segments yet.
    if not e_certificate_value:
        return {"key": "market", "active": False}
    lower_factor, upper_factor = 0.75, 1.25
    return {
        "key": "market",
        "label": "повний робочий коридор",
        "active": True,
        "benchmark": round(e_certificate_value),
        "minimum": round(e_certificate_value * lower_factor),
        "maximum": round(e_certificate_value * upper_factor),
        "centre": round(e_certificate_value * ((lower_factor + upper_factor) / 2)),
        "corridor_minimum": round(e_certificate_value * 0.75),
        "corridor_maximum": round(e_certificate_value * 1.25),
        "method": "e_certificate_hard_corridor_then_property_similarity",
    }


def _apply_report_valuation(report: Report, statistics: dict, options: dict, e_certificate_value: float) -> dict:
    """Apply declared trade and e-certificate controls to selected-market statistics."""
    statistics = dict(statistics)
    trade_percent = float(options.get("trade_adjustment_percent") or 0)
    if trade_percent < 0 or trade_percent > 15:
        raise HTTPException(400, "Коригування на торг може бути від 0 до 15%")
    if trade_percent and not str(options.get("trade_adjustment_reason") or "").strip():
        raise HTTPException(400, "Для коригування на торг вкажіть професійне обґрунтування")
    if trade_percent:
        factor = 1 - trade_percent / 100
        for key in ("recommended_value", "range_min", "range_max"):
            statistics[key] = round(float(statistics[key]) * factor)
        statistics["market_value_before_trade"] = round(float(statistics["recommended_value"]) / factor)
        statistics["trade_adjustment_percent"] = trade_percent
        statistics["trade_adjustment_reason"] = str(options.get("trade_adjustment_reason") or "").strip()

    if e_certificate_value:
        has_e_certificate_scan = bool(report.e_certificate_files or [])
        if not has_e_certificate_scan and not bool(options.get("e_certificate_manual_confirmed")):
            raise HTTPException(400, "Додайте скан е-довідки або підтвердьте, що суму введено вручну та перевірено оцінювачем")
        market_recommended = round(float(statistics["recommended_value"]))
        statistics.update({
            "e_certificate_value": round(e_certificate_value),
            "e_certificate_source": "attached_scan" if has_e_certificate_scan else "manual_confirmed",
            "market_vs_e_certificate_percent": round((market_recommended - e_certificate_value) / e_certificate_value * 100, 2),
            "range_source": "e_certificate_plus_minus_25",
            "range_min": round(e_certificate_value * 0.75),
            "range_max": round(e_certificate_value * 1.25),
            "market_recommended_value": market_recommended,
            "recommended_value": round(e_certificate_value),
            "recommended_value_origin": "e_certificate_benchmark",
        })
    return statistics


def _price_position_for_report(report: Report) -> dict | None:
    """Describe where inside the substantiated range the appraiser placed the
    final value (lower / middle / upper), for the optional expert-text
    price-consistency guidance.

    Purely derived from values already stored on the report
    (selected_value/estimated_value vs range_min/range_max/recommended_value)
    -- no new database column and no new request field. Returns None whenever
    the numbers are missing or degenerate, in which case the expert-text
    generator keeps its previous behaviour (no price guidance injected).
    """
    def _num(value):
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    lo = _num(report.range_min)
    hi = _num(report.range_max)
    rec = _num(report.recommended_value)
    sel = _num(report.selected_value)
    if sel is None:
        sel = _num(report.estimated_value)
    if lo is None or hi is None or sel is None or hi <= lo:
        return None

    # Clamp into the range so a value stored slightly outside (rounding, an
    # e-certificate corridor edge) still yields a sane 0..1 position.
    clamped = min(max(sel, lo), hi)
    rel = (clamped - lo) / (hi - lo)
    if rel <= 0.33:
        position = "lower"
    elif rel >= 0.67:
        position = "upper"
    else:
        position = "middle"

    return {
        "position": position,
        "range_min": round(lo),
        "range_max": round(hi),
        "recommended_value": round(rec) if rec is not None else None,
        "selected_value": round(sel),
    }


def _as_positive_int(value) -> Optional[int]:
    """Convert OCR/provider numeric text before assigning an INTEGER column."""
    number = _as_float(value, 0.0)
    return int(number) if number > 0 else None


def _db_text(value, limit: int) -> str:
    """Keep provider prose in raw_data while respecting database field limits.

    Marketplaces occasionally return a full sales description as a title.
    The report only needs a readable caption; the untouched provider payload
    stays in ``Analog.raw_data`` for audit and diagnostic purposes.
    """
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# OCR may return Ukrainian labels, English labels, or the legacy ``land_plot``
# value.  The selected report type is never changed automatically: this only
# prevents a wrong marketplace search from being launched.
OBJECT_TYPE_LABELS = {
    "apartment": "\u041a\u0432\u0430\u0440\u0442\u0438\u0440\u0430",
    "house": "\u0416\u0438\u0442\u043b\u043e\u0432\u0438\u0439 \u0431\u0443\u0434\u0438\u043d\u043e\u043a",
    "land": "\u0417\u0435\u043c\u0435\u043b\u044c\u043d\u0430 \u0434\u0456\u043b\u044f\u043d\u043a\u0430",
}
OBJECT_TYPE_LABELS["commercial"] = "Комерційний об'єкт"


def _canonical_object_type(value) -> str | None:
    text = " ".join(str(value or "").lower().replace("’", "'").split())
    aliases = {
        "apartment": {"apartment", "flat", "\u043a\u0432\u0430\u0440\u0442\u0438\u0440\u0430", "\u043a\u0432\u0430\u0440\u0442\u0438\u0440\u0438"},
        "house": {"house", "private_house", "residential_house", "\u0431\u0443\u0434\u0438\u043d\u043e\u043a", "\u0436\u0438\u0442\u043b\u043e\u0432\u0438\u0439 \u0431\u0443\u0434\u0438\u043d\u043e\u043a", "\u0441\u0430\u0434\u0438\u0431\u043d\u0438\u0439 \u0431\u0443\u0434\u0438\u043d\u043e\u043a"},
        "land": {"land", "land_plot", "parcel", "plot", "\u0434\u0456\u043b\u044f\u043d\u043a\u0430", "\u0437\u0435\u043c\u0435\u043b\u044c\u043d\u0430 \u0434\u0456\u043b\u044f\u043d\u043a\u0430"},
    }
    commercial_aliases = {
        "commercial", "commercial_property", "non_residential",
        "нежитлове приміщення", "офіс", "магазин", "склад",
        "виробниче приміщення",
    }
    if text in commercial_aliases:
        return "commercial"
    return next((canonical for canonical, values in aliases.items() if text in values), None)


def _object_type_mismatch(selected, detected) -> bool:
    return bool(selected and detected and selected != detected)


def _detected_object_type_from_evidence(ocr_data: dict) -> str | None:
    """Prefer an explicit flat/unit reference over an AI classification.

    A document address containing ``кв. 49`` is direct evidence of an
    apartment.  It must not be overridden by a hallucinated ``house`` label.
    The original AI label is retained in the OCR audit payload by the caller.
    """
    ai_type = _canonical_object_type((ocr_data or {}).get("object_type"))
    address = str((ocr_data or {}).get("address") or "")
    apartment = (ocr_data or {}).get("apartment")
    if apartment not in (None, "") or re.search(r"\b(?:кв(?:артира)?\.?)\s*(?:№\s*)?\d+", address, re.IGNORECASE):
        return "apartment"
    return ai_type


# ========== AUTH ==========

@auth_router.post("/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Перевірка чи email вже зайнятий
    existing = await db.execute(select(User).where(User.email == req.email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Ця електронна пошта вже зареєстрована")
    
    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
        full_name=req.full_name,
        phone=req.phone,
        cert_number=req.cert_number,
        sod_name=req.sod_name,
        sod_edrpou=req.sod_edrpou,
        sod_cert_number=req.sod_cert_number,
        sod_address=req.sod_address,
        sod_header_offset_mm=req.sod_header_offset_mm or 0,
        newsletter_consent=req.newsletter_consent,
        free_reports_left=3,
        plan=SubscriptionPlan.FREE,
    )
    db.add(user)
    await db.flush()
    
    # Лог
    db.add(ActivityLog(user_id=user.id, action="register", details={"email": req.email}))
    
    token = create_token(str(user.id))
    return {"token": token, "user_id": str(user.id), "name": user.full_name}


@auth_router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Невірна пошта або пароль")
    
    user.last_login = datetime.utcnow()
    db.add(ActivityLog(user_id=user.id, action="login"))
    
    token = create_token(str(user.id))
    return {"token": token, "user_id": str(user.id), "name": user.full_name}


@auth_router.get("/me")
async def me(user: User = Depends(get_current_user)):
    plan_value = user.plan.value if hasattr(user.plan, "value") else str(user.plan or "free")
    plan_limits = {"free": 3, "basic": 10, "standard": 30, "pro": 60, "team": 9999}
    plan_limit = plan_limits.get(plan_value, 3)
    is_free = plan_value == SubscriptionPlan.FREE.value
    active_until = user.subscription_active_until
    subscription_active = bool(user.is_admin) or is_free or bool(active_until and active_until >= datetime.utcnow())
    reports_used = (3 - max(0, user.free_reports_left or 0)) if is_free else (user.reports_this_month or 0)
    reports_left = max(0, user.free_reports_left or 0) if is_free else max(0, plan_limit - reports_used)
    return {
        "id": str(user.id), "email": user.email, "name": user.full_name,
        "phone": user.phone, "plan": user.plan, "free_reports_left": user.free_reports_left,
        "reports_this_month": user.reports_this_month,
        "plan_limit": plan_limit, "reports_used": reports_used, "reports_left": reports_left,
        "subscription_active": subscription_active,
        "subscription_active_until": active_until.isoformat() if active_until else None,
        "is_admin": bool(user.is_admin),
        "cert_number": user.cert_number,
        "sod_name": user.sod_name, "sod_edrpou": user.sod_edrpou,
        "sod_cert_number": user.sod_cert_number, "sod_address": user.sod_address,
        "sod_header_offset_mm": user.sod_header_offset_mm or 0,
        "profile_documents": [_profile_document_public(item) for item in (user.profile_document_files or [])],
    }


@auth_router.patch("/me")
async def update_profile(
    req: ProfileUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save the evaluator profile; future reports take these as defaults."""
    user.full_name = req.full_name.strip()
    user.phone = (req.phone or "").strip() or None
    user.cert_number = (req.cert_number or "").strip() or None
    user.sod_name = (req.sod_name or "").strip() or None
    user.sod_edrpou = (req.sod_edrpou or "").strip() or None
    user.sod_cert_number = (req.sod_cert_number or "").strip() or None
    user.sod_address = (req.sod_address or "").strip() or None
    user.sod_header_offset_mm = max(0, min(int(req.sod_header_offset_mm or 0), 120))
    db.add(ActivityLog(user_id=user.id, action="profile_updated"))
    await db.commit()
    return {"ok": True, "message": "Дані профілю збережено"}


@auth_router.post("/me/documents")
async def upload_profile_documents(
    files: list[UploadFile] = File(...),
    document_kind: str = Form("other"),
    replace: bool = Form(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Store evaluator/SOD scans separately from the documents of an object."""
    if document_kind not in PROFILE_DOCUMENT_KINDS:
        raise HTTPException(400, "Непідтримувана категорія документа")
    target = Path(settings.profile_documents_dir) / str(user.id)
    target.mkdir(parents=True, exist_ok=True)
    stored = list(user.profile_document_files or [])
    if replace:
        retained = []
        for item in stored:
            if item.get("kind") == document_kind:
                try:
                    Path(item.get("path", "")).unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                retained.append(item)
        stored = retained
    added = 0
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if document_kind == "sod_logo" and suffix not in {".jpg", ".jpeg", ".png"}:
            continue
        if suffix not in {".pdf", ".jpg", ".jpeg", ".png"}:
            continue
        content = await file.read()
        if not content:
            continue
        if len(content) > 25 * 1024 * 1024:
            raise HTTPException(413, "Один файл не може перевищувати 25 МБ")
        document_id = str(uuid.uuid4())
        path = target / f"{document_id}{suffix}"
        path.write_bytes(content)
        entry = {
            "id": document_id,
            "name": Path(file.filename or f"document{suffix}").name[:180],
            "kind": document_kind,
            "path": str(path),
            "uploaded_at": datetime.utcnow().isoformat(),
        }
        # Certificates and statutory scans later become report annexes. Read
        # their page orientation once at upload time, rather than guessing
        # during every PDF build. This metadata is presentation-only.
        if document_kind != "sod_logo":
            try:
                profile_ocr = await process_document(str(path))
                entry["page_orientations"] = list(profile_ocr.get("page_orientations") or [])
                print(
                    f"Profile document orientation saved: file={path.name}, "
                    f"pages={len(entry['page_orientations'])}",
                    flush=True,
                )
            except Exception as error:
                # A valid document upload must not fail if an OCR provider is
                # briefly unavailable; local orientation remains a fallback.
                entry["page_orientations"] = []
                print("Profile document orientation unavailable: " + str(error), flush=True)
        stored.append(entry)
        added += 1
    if not added:
        raise HTTPException(400, "Завантажте PDF, JPG або PNG до 25 МБ")
    user.profile_document_files = stored
    db.add(ActivityLog(user_id=user.id, action="profile_documents_uploaded", details={"kind": document_kind, "count": added, "replace": replace}))
    await db.commit()
    return {"documents": [_profile_document_public(item) for item in stored]}


@auth_router.delete("/me/documents/{document_id}")
async def delete_profile_document(document_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stored = list(user.profile_document_files or [])
    kept, deleted = [], None
    for item in stored:
        if item.get("id") == document_id:
            deleted = item
        else:
            kept.append(item)
    if not deleted:
        raise HTTPException(404, "Документ не знайдено")
    try:
        Path(deleted.get("path", "")).unlink(missing_ok=True)
    except OSError:
        pass
    user.profile_document_files = kept
    db.add(ActivityLog(user_id=user.id, action="profile_document_deleted", details={"document_id": document_id}))
    await db.commit()
    return {"documents": [_profile_document_public(item) for item in kept]}


@auth_router.get("/me/documents/{document_id}/download")
async def download_profile_document(document_id: str, user: User = Depends(get_current_user)):
    item = next((entry for entry in (user.profile_document_files or []) if entry.get("id") == document_id), None)
    path = Path(item.get("path", "")) if item else None
    if not item or not path or not path.is_file():
        raise HTTPException(404, "Документ не знайдено")
    return FileResponse(path, filename=item.get("name", path.name))


# ========== REPORTS ==========

@reports_router.post("/create")
async def create_report(
    req: CreateReportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Створити новий звіт (без файлів поки що)"""
    # New evaluators receive exactly three completed draft generations.
    # The service account administrator is the only unlimited exception.
    plan_limits = {"free": 3, "basic": 10, "standard": 30, "pro": 60, "team": 9999}
    plan_value = user.plan.value if hasattr(user.plan, "value") else str(user.plan or "free")
    limit = plan_limits.get(plan_value, 3)

    if not user.is_admin and plan_value == SubscriptionPlan.FREE.value:
        if user.free_reports_left <= 0:
            raise HTTPException(403, "Три безкоштовні генерації вичерпано. Для наступного звіту оберіть та оплатіть пакет.")
    elif not user.is_admin:
        if not user.subscription_active_until or user.subscription_active_until < datetime.utcnow():
            raise HTTPException(403, "Пакет не активний. Для створення звіту потрібна підтверджена оплата.")
        if (user.reports_this_month or 0) >= limit:
            raise HTTPException(403, f"Ліміт пакета: {limit} звітів на місяць. Оберіть наступний пакет.")
    
    if req.object_type not in {"apartment", "house", "land"}:
        raise HTTPException(400, "Підтримуються лише: квартира, житловий будинок і земельна ділянка")
    report = Report(
        user_id=user.id,
        object_type=ObjectType(req.object_type),
        deal_type=DealType(req.deal_type),
        eval_mode=EvalMode(req.eval_mode),
        status=ReportStatus.UPLOADING,
        expires_at=datetime.utcnow() + timedelta(days=settings.report_retention_days),
    )
    db.add(report)
    await db.flush()
    
    db.add(ActivityLog(user_id=user.id, action="create_report", details={"report_id": str(report.id)}))
    
    return {"report_id": str(report.id), "status": report.status}


@reports_router.post("/{report_id}/upload")
async def upload_documents(
    report_id: str,
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Завантажити документи та запустити OCR"""
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    
    # Зберегти файли
    # Source documents remain separate from object photographs and generated files.
    # This stable layout is ready for a later off-server backup policy.
    print(f"Upload received: report={report.id}, files={len(files)}", flush=True)
    upload_dir = os.path.join(settings.upload_dir, str(user.id), str(report.id), "documents")
    os.makedirs(upload_dir, exist_ok=True)
    
    saved_files = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ['.pdf', '.jpg', '.jpeg', '.png']:
            continue
        fname = f"{uuid.uuid4()}{ext}"
        fpath = os.path.join(upload_dir, fname)
        content = await f.read()
        Path(fpath).write_bytes(content)
        saved_files.append(fpath)
    
    print(f"Upload saved: report={report.id}, supported_files={len(saved_files)}", flush=True)
    if not saved_files:
        raise HTTPException(400, "Не завантажено жодного підтримуваного файлу (PDF, JPG, PNG)")
    
    report.upload_files = saved_files
    report.status = ReportStatus.OCR_PROCESSING
    # Persist the upload before calling an external AI provider.  If Gemini is
    # temporarily unavailable or the request is interrupted, the user must be
    # able to resume from the saved documents instead of receiving an empty
    # draft report.
    await db.commit()
    
    # Extract all source documents, then let Claude Opus compare them. The
    # returned conflicts are deliberately preserved for appraiser review.
    try:
        ocr_result = await process_documents(saved_files)
    except Exception as error:
        print(f"OCR processing failed: report={report.id}, error={error}", flush=True)
        report.status = ReportStatus.OCR_FAILED
        report.error_message = str(error)[:1000]
        await db.commit()
        raise HTTPException(502, "Не вдалося обробити документи. Файли збережено; спробуйте продовжити пізніше.")
    selected_object_type = _canonical_object_type(
        report.object_type.value if hasattr(report.object_type, "value") else report.object_type
    )
    ai_detected_object_type = _canonical_object_type(ocr_result.get("object_type"))
    detected_object_type = _detected_object_type_from_evidence(ocr_result)
    if ai_detected_object_type and ai_detected_object_type != detected_object_type:
        ocr_result["_ai_detected_object_type"] = ai_detected_object_type
        ocr_result["_detected_object_type_source"] = "explicit apartment reference in object address"
    type_mismatch = _object_type_mismatch(selected_object_type, detected_object_type)
    # Keep both values in the audit data.  Do not silently replace the type
    # selected by the evaluator with an AI result.
    ocr_result["_selected_object_type"] = selected_object_type
    ocr_result["_detected_object_type"] = detected_object_type
    ocr_result["_object_type_mismatch"] = type_mismatch
    report.ocr_raw = ocr_result
    report.ocr_provider = ocr_result.get("_provider", "unknown")
    
    # Заповнення даних звіту з OCR
    if ocr_result.get("confidence", 0) > 0:
        report.address = ocr_result.get("address") or report.address
        area_sqm = _as_float(ocr_result.get("area_total"), 0.0)
        if not area_sqm:
            # Земельна ділянка: OCR кладе розмір не в area_total (це поле для
            # площі будівлі), а окремо в land_area_sqm / land_area_ha. Без
            # цього фолбеку report.area_sqm лишався 0 для будь-якої ділянки,
            # а /find-analogs жорстко відхиляє пошук аналогів, коли площа не
            # визначена (HTTPException 422 "Не визначено загальну площу
            # об'єкта") — тобто пошук аналогів по землі не запускався взагалі.
            area_sqm = _as_float(ocr_result.get("land_area_sqm"), 0.0)
            if not area_sqm:
                land_area_ha = _as_float(ocr_result.get("land_area_ha"), 0.0)
                if land_area_ha:
                    area_sqm = land_area_ha * 10000
        rooms = _as_positive_int(ocr_result.get("rooms"))
        floor = _as_positive_int(ocr_result.get("floor"))
        total_floors = _as_positive_int(ocr_result.get("total_floors"))
        year_built = _as_positive_int(ocr_result.get("year_built"))
        report.area_sqm = area_sqm or report.area_sqm
        report.rooms = rooms or report.rooms
        report.floor = floor or report.floor
        report.total_floors = total_floors or report.total_floors
        report.year_built = year_built or report.year_built
        report.cadastral_number = ocr_result.get("cadastral_number") or report.cadastral_number
    
    # A scan of a house must never launch apartment searches (or vice versa).
    # The source files remain saved, but a new draft with the right type is
    # required before any external marketplace request is made.
    if type_mismatch:
        report.status = ReportStatus.OCR_FAILED
        db.add(ActivityLog(user_id=user.id, action="object_type_mismatch", details={
            "report_id": str(report.id),
            "selected_object_type": selected_object_type,
            "detected_object_type": detected_object_type,
        }))
        print(
            f"Object type mismatch: report={report.id}, selected={selected_object_type}, detected={detected_object_type}",
            flush=True,
        )
        await db.commit()
        return {
            "report_id": str(report.id),
            "status": "ocr_object_type_mismatch",
            "ocr_data": ocr_result,
            "object_type_mismatch": True,
            "selected_object_type": selected_object_type,
            "detected_object_type": detected_object_type,
            "message": (
                "\u0423 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0430\u0445 \u0440\u043e\u0437\u043f\u0456\u0437\u043d\u0430\u043d\u043e \u00ab"
                + OBJECT_TYPE_LABELS.get(detected_object_type, str(ocr_result.get("object_type") or "\u0456\u043d\u0448\u0438\u0439 \u043e\u0431'\u0454\u043a\u0442"))
                + "\u00bb, \u0430\u043b\u0435 \u043d\u0430 \u0441\u0430\u0439\u0442\u0456 \u043e\u0431\u0440\u0430\u043d\u043e \u00ab"
                + OBJECT_TYPE_LABELS.get(selected_object_type, "\u0456\u043d\u0448\u0438\u0439 \u0442\u0438\u043f")
                + "\u00bb. \u041f\u043e\u0448\u0443\u043a \u0430\u043d\u0430\u043b\u043e\u0433\u0456\u0432 \u043d\u0435 \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u043e."
            ),
        }

    # Conflicts stay in the audit trail and are visibly marked in generated
    # drafts.  They should not block analogue search; the appraiser checks the
    # marked fields before signing or registering a report.
    report.status = ReportStatus.ANALOGS_SEARCH
    
    db.add(ActivityLog(user_id=user.id, action="upload", details={
        "report_id": str(report.id), "files": len(saved_files), "ocr_provider": report.ocr_provider,
        "selected_object_type": selected_object_type, "detected_object_type": detected_object_type,
    }))
    
    return {
        "report_id": str(report.id),
        "status": "ocr_done_with_warnings" if ocr_result.get("_needs_review") else "ocr_done",
        "ocr_data": ocr_result,
        "warning": "Виявлені розбіжності буде виділено у чернетці звіту для перевірки оцінювачем." if ocr_result.get("_needs_review") else None,
    }


@reports_router.post("/{report_id}/confirm-ocr")
async def confirm_ocr_data(
    report_id: str,
    req: ConfirmOcrRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Allow the appraiser to confirm resolved OCR data without re-uploading files.

    The original conflicts remain immutable in ocr_raw and ActivityLog.  This
    acknowledgement is not an automatic legal decision: it records that the
    licensed appraiser reviewed the source documents before market search.
    """
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    if not report.ocr_raw:
        raise HTTPException(400, "Спочатку завантажте документи")

    ocr_data = dict(report.ocr_raw)
    ocr_data["_needs_review"] = False
    ocr_data["_appraiser_confirmed_at"] = datetime.utcnow().isoformat()
    ocr_data["_appraiser_confirmation_note"] = (req.note or "").strip() or None
    report.ocr_raw = ocr_data
    report.status = ReportStatus.ANALOGS_SEARCH
    db.add(ActivityLog(user_id=user.id, action="confirm_ocr_data", details={
        "report_id": str(report.id),
        "conflict_fields": [item.get("field") for item in ocr_data.get("_conflicts", [])],
        "note": ocr_data["_appraiser_confirmation_note"],
    }))

    # Duplicate detection (warn only). If this appraiser already has a report
    # for the SAME property, surface it so the frontend can say "такий звіт вже
    # є" and offer Продовжити наявний / Створити новий. Match on the cadastral
    # number — the reliable unique key; the OCR address is too inconsistent to
    # match on (same object came out as "м. Борзна" and "Борзна, Ніжинський
    # район, Чернігівська область"). As a softer fallback, when there is no
    # cadastral number, match an exact normalized address + the same area. This
    # never blocks — the appraiser decides; confirm-ocr already succeeded.
    duplicates = []
    cad = (report.cadastral_number or "").strip()
    if cad:
        dup_q = select(Report).where(
            Report.user_id == user.id, Report.id != report.id,
            Report.cadastral_number == cad,
        ).order_by(Report.created_at.desc()).limit(5)
        match_reason = "cadastral_number"
    elif report.address:
        dup_q = select(Report).where(
            Report.user_id == user.id, Report.id != report.id,
            func.lower(func.trim(Report.address)) == report.address.strip().lower(),
            Report.area_sqm == report.area_sqm,
        ).order_by(Report.created_at.desc()).limit(5)
        match_reason = "address_area"
    else:
        dup_q = None
        match_reason = None
    if dup_q is not None:
        for d in (await db.execute(dup_q)).scalars().all():
            duplicates.append({
                "id": str(d.id),
                "address": d.address,
                "status": d.status.value if hasattr(d.status, "value") else d.status,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "has_word": bool(d.word_path),
                "next_step": _report_next_step(d.status, bool(d.word_path or d.pdf_full_path)),
            })

    return {
        "report_id": str(report.id),
        "status": "ocr_confirmed",
        "ocr_data": ocr_data,
        # Non-empty → frontend shows "такий звіт уже є" with Продовжити/Новий.
        "duplicate_of": duplicates,
        "duplicate_match": match_reason if duplicates else None,
    }


def _analog_locality(item) -> str:
    """Best-effort town name for one analog, for display only.

    Never used in any calculation: it exists so the appraiser can see at a
    glance whether a comparable is from the same settlement as the subject.
    """
    import re as _re
    raw = item.raw_data or {}
    for key in ("city", "district", "city_name_uk", "city_name"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    # OLX carries no locality field; its URLs usually contain the town.
    url = str(raw.get("url") or getattr(item, "url", "") or "")
    match = _re.search(r"realty-prodaja-\w+-([a-z-]+?)-\d+\.html", url)
    if match:
        return match.group(1).replace("-", " ").title()
    # Fall back to the stored address, which begins with the city when known.
    address = str(getattr(item, "address", "") or "").strip()
    return address.split(",")[0].strip() if address else ""


@reports_router.post("/{report_id}/find-analogs")
async def find_analogs(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Пошук ринкових аналогів через DIM.RIA"""
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    
    options = report.report_options or {}
    city = (options.get("manual_city") or "").strip()
    if not city and report.ocr_raw and report.ocr_raw.get("city"):
        city = report.ocr_raw["city"]
    elif report.ocr_raw:
        city = infer_city_from_extracts(report.ocr_raw.get("_document_extracts", [])) or ""
    if not city and report.address:
        # Спроба витягнути місто з адреси
        city = infer_city_from_extracts([{"address": report.address}]) or ""
    
    if not city:
        raise HTTPException(400, "Не вдалося визначити місто. Вкажіть адресу вручну.")
    
    from app.services.analog_search import search_all
    from app.services.valuation_analytics import analyse
    object_type = report.object_type.value if hasattr(report.object_type, "value") else report.object_type
    selected_object_type = _canonical_object_type(object_type)
    detected_object_type = _detected_object_type_from_evidence(report.ocr_raw or {})
    if _object_type_mismatch(selected_object_type, detected_object_type):
        raise HTTPException(
            409,
            "\u0422\u0438\u043f \u043e\u0431'\u0454\u043a\u0442\u0430 \u0432 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0430\u0445 \u043d\u0435 \u0437\u0431\u0456\u0433\u0430\u0454\u0442\u044c\u0441\u044f \u0437 \u043e\u0431\u0440\u0430\u043d\u0438\u043c. \u041f\u043e\u0448\u0443\u043a \u0430\u043d\u0430\u043b\u043e\u0433\u0456\u0432 \u0437\u0430\u0431\u043b\u043e\u043a\u043e\u0432\u0430\u043d\u043e. \u0421\u0442\u0432\u043e\u0440\u0456\u0442\u044c \u043d\u043e\u0432\u0438\u0439 \u0437\u0432\u0456\u0442 \u0437 \u043f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u0438\u043c \u0442\u0438\u043f\u043e\u043c \u043e\u0431'\u0454\u043a\u0442\u0430.",
        )
    if str(object_type).lower() in {"apartment", "квартира"} and not report.rooms:
        raise HTTPException(
            422,
            "Не визначено кількість кімнат у технічних даних. Пошук не запускається, щоб не підміняти аналоги 3- або 4-кімнатними квартирами. Додайте сторінку техпаспорта з експлікацією або уточніть дані оцінювачем.",
        )
    if not report.area_sqm:
        raise HTTPException(422, "Не визначено загальну площу об'єкта. Пошук аналогів без неї не запускається.")
    subject = {"city": city, "object_type": object_type,
               "area_sqm": report.area_sqm, "rooms": report.rooms, "floor": report.floor, "total_floors": report.total_floors, "year_built": report.year_built,
               # district is an oblast in legacy OCR; only neighborhood is a city-level comparable factor.
               "district": (report.ocr_raw or {}).get("neighborhood"),
               # DIM.RIA requires a state ID to resolve a non-oblast-centre
               # city.  OCR's legacy district field often carries the oblast,
               # so it is used only for that technical lookup, never to rank
               # city neighbourhoods as though they were the same thing.
               "region": ((report.ocr_raw or {}).get("region") or (report.ocr_raw or {}).get("oblast") or (report.ocr_raw or {}).get("district"))}
    # The e-certificate value is a mandatory working corridor when entered.
    # Pass it to the sources before any cards are opened, then apply the same
    # guard again below because external adapters may change independently.
    e_certificate_value = _as_float(options.get("e_certificate_value"), 0)
    # Legacy clients may still send lower/center/upper.  Never allow that
    # stale UI value to narrow the initial candidate search.
    search_price_band = "market"
    price_band = _search_price_band(e_certificate_value)
    price_min_uah = price_band.get("corridor_minimum") if price_band.get("active") else None
    price_max_uah = price_band.get("corridor_maximum") if price_band.get("active") else None
    print(f"Analog search started: report={report.id}, subject={subject}")
    try:
        # Keep the HTTP response inside the nginx timeout and preserve a clear
        # diagnostic record instead of leaving the user on an endless spinner.
        raw_analogs, search_journal = await asyncio.wait_for(
            # Candidate review may inspect up to 48 OLX cards before strict
            # comparability filtering.  Keep the HTTP request bounded, but do
            # not abort a legitimate 10–12-candidate search at the old 85 s.
            search_all(
                subject,
                settings,
                include_olx=bool((report.report_options or {}).get("include_olx", True)),
                price_min_uah=price_min_uah,
                price_max_uah=price_max_uah,
            ), timeout=280
        )
    except asyncio.TimeoutError:
        print(f"Analog search timed out: report={report.id}")
        raise HTTPException(504, "Пошук аналогів перевищив безпечний час очікування. Чернетку збережено; повторіть спробу пізніше.")
    except Exception:
        print(f"Analog search failed: report={report.id}")
        traceback.print_exc()
        raise HTTPException(500, "Помилка пошуку аналогів. Дані чернетки збережено.")

    if len(raw_analogs) < 1:
        return {"report_id": str(report.id), "analogs_found": 0, "warning": "Аналогів не знайдено"}

    # Marketplace adapters sometimes return a formatted currency string such
    # as "46,000 грн".  Normalize before filtering and analytics: otherwise
    # it is read as 46 грн and corrupts the selected-comparables table.
    normalized_analogs = []
    invalid_price_candidates = 0
    for item in raw_analogs:
        candidate = dict(item or {})
        price = _as_float(candidate.get("price_uah"))
        area = _as_float(candidate.get("area_sqm"))
        if price < 5_000 or (area > 0 and price / area < 300):
            invalid_price_candidates += 1
            continue
        candidate["price_uah"] = round(price)
        if area > 0:
            candidate["area_sqm"] = area
            candidate["price_per_sqm"] = round(price / area)
        normalized_analogs.append(candidate)
    raw_analogs = normalized_analogs
    search_journal["invalid_price_candidates_removed"] = invalid_price_candidates
    if not raw_analogs:
        return {
            "report_id": str(report.id),
            "analogs_found": 0,
            "warning": "Не знайдено оголошень з коректно визначеною ціною.",
            "search_journal": search_journal,
        }

    outside_certificate_corridor = 0
    if price_min_uah is not None and price_max_uah is not None:
        corridor_analogs = []
        for item in raw_analogs:
            price = _as_float(item.get("price_uah"))
            if price_min_uah <= price <= price_max_uah:
                corridor_analogs.append(item)
            else:
                outside_certificate_corridor += 1
        raw_analogs = corridor_analogs
    search_journal["e_certificate_price_corridor"] = {
        "active": bool(price_min_uah is not None and price_max_uah is not None),
        "minimum_uah": round(price_min_uah) if price_min_uah is not None else None,
        "maximum_uah": round(price_max_uah) if price_max_uah is not None else None,
        "removed": outside_certificate_corridor,
        "remaining": len(raw_analogs),
    }
    if not raw_analogs:
        return {
            "report_id": str(report.id),
            "analogs_found": 0,
            "warning": (
                "У робочому ціновому коридорі е-довідки не знайдено зіставних "
                "оголошень. Пошук завершено без підміни аналогів об'єктами поза "
                "заданими критеріями."
            ),
            "search_journal": search_journal,
        }

    # Defence in depth: external adapters may be updated independently.  Do
    # not let an adapter that forgets a filter put a 3- or 4-room flat into a
    # two-room valuation. Room count stays an exact, non-negotiable gate.
    # Area is intentionally NOT enforced here (product decision): only rooms
    # and the price corridor are strict; area is a ranking/weighting signal
    # downstream in valuation_analytics, not a rejection criterion.
    strict_removed = 0
    if str(object_type).lower() in {"apartment", "квартира"}:
        required_rooms = int(_as_float(report.rooms, -1))
        required_area = _as_float(report.area_sqm, 0)
        strict_analogs = []
        for item in raw_analogs:
            candidate_rooms = int(_as_float(item.get("rooms"), -1))
            candidate_area = _as_float(item.get("area_sqm"), 0)
            comparable = candidate_rooms == required_rooms and candidate_area > 0
            if comparable:
                strict_analogs.append(item)
            else:
                strict_removed += 1
        raw_analogs = strict_analogs
        search_journal["strict_apartment_filter"] = {
            "rooms": required_rooms,
            "area_sqm": required_area,
            "area_enforced": False,
            "removed": strict_removed,
            "remaining": len(raw_analogs),
        }
        # A thin market remains reviewable. Only an empty same-room pool must
        # stop the calculation; the appraiser sees the smaller honest sample.
        if len(raw_analogs) < 1:
            return {
                "report_id": str(report.id),
                "analogs_found": 0,
                "warning": (
                    f"Знайдено 0 зіставних {required_rooms}-кімнатних оголошень. "
                    f"Пошук зупинено, щоб не підміняти їх іншими квартирами."
                ),
                "search_journal": search_journal,
            }
    
    # Prepare a neutral candidate pool for the licensed appraiser to review.
    # Final selection happens before valuation and screenshot capture.
    analysis = analyse(raw_analogs, subject, selection_limit=12, price_band=price_band)
    if not analysis.selected:
        return {"report_id": str(report.id), "analogs_found": 0, "warning": "Недостатньо повних даних для аналізу аналогів"}
    # A thin local market can legitimately leave fewer than three comparable
    # cards. Per product policy this must surface as a visible warning to the
    # appraiser, not a silent selection screen the evaluator cannot confirm
    # (the UI and /select-analogs both required exactly 3-5 previously).
    low_candidate_pool = len(analysis.selected) < 3
    search_journal.update(analysis.journal)
    report.search_journal = search_journal
    statistics = dict(analysis.statistics)

    # A negotiation adjustment is a documented professional assumption.  It
    # must never be silently treated as a universal legal 5–10% rule.
    options = report.report_options or {}
    trade_percent = float(options.get("trade_adjustment_percent") or 0)
    if trade_percent < 0 or trade_percent > 15:
        raise HTTPException(400, "Коригування на торг може бути від 0 до 15%")
    if trade_percent and not str(options.get("trade_adjustment_reason") or "").strip():
        raise HTTPException(400, "Для коригування на торг вкажіть професійне обґрунтування")
    factor = 1 - trade_percent / 100
    if trade_percent:
        for key in ("recommended_value", "range_min", "range_max"):
            statistics[key] = round(float(statistics[key]) * factor)
        statistics["market_value_before_trade"] = analysis.statistics["recommended_value"]
        statistics["trade_adjustment_percent"] = trade_percent
        statistics["trade_adjustment_reason"] = str(options.get("trade_adjustment_reason") or "").strip()

    # An e-certificate is attached by the appraiser.  If its amount is
    # entered, display the permitted working corridor around that document;
    # otherwise keep the evidence-based market range.
    if e_certificate_value:
        has_e_certificate_scan = bool(report.e_certificate_files or [])
        if not has_e_certificate_scan and not bool(options.get("e_certificate_manual_confirmed")):
            raise HTTPException(
                400,
                "Додайте скан е-довідки або підтвердьте, що суму введено вручну та перевірено оцінювачем",
            )
        statistics["e_certificate_value"] = round(e_certificate_value)
        statistics["e_certificate_source"] = "attached_scan" if has_e_certificate_scan else "manual_confirmed"
        statistics["market_vs_e_certificate_percent"] = round(
            (statistics["recommended_value"] - e_certificate_value) / e_certificate_value * 100, 2
        )
        statistics["range_source"] = "e_certificate_plus_minus_25"
        statistics["range_min"] = round(e_certificate_value * 0.75)
        statistics["range_max"] = round(e_certificate_value * 1.25)
        # The e-certificate is the selected benchmark for this workflow.
        # Its entered amount is therefore the centre/recommended value and the
        # ±25% corridor is calculated around it.  Keep the market result for
        # audit and comparison, but never let it replace the confirmed centre
        # of the displayed state-reference corridor.
        market_recommended = round(float(statistics["recommended_value"]))
        statistics["market_recommended_value"] = market_recommended
        statistics["recommended_value"] = round(e_certificate_value)
        statistics["recommended_value_origin"] = "e_certificate_benchmark"
        statistics["search_price_band"] = price_band
        search_journal["price_ranking"] = price_band
    report.valuation_statistics = statistics
    
    # A restart of this operation for the same draft must replace its market
    # sample, not append duplicate cards and change the median unpredictably.
    await db.execute(delete(Analog).where(Analog.report_id == report.id))
    saved = []
    screenshots_requested = _screenshots_enabled(report.report_options or {})
    for idx, a in enumerate(analysis.selected, 1):
        # Marketplace responses are external input: prices/areas can be strings.
        # A malformed card must be skipped by analytics, never crash the report.
        price = _as_float(a.get("price_uah"))
        area = _as_float(a.get("area_sqm"))
        
        analog = Analog(
            report_id=report.id,
            source=_db_text(a.get("source", "dimria"), 50),
            url=_db_text(a.get("url", ""), 1000),
            title=_db_text(a.get("title", ""), 500),
            price_uah=price,
            price_per_sqm=price / area if area > 0 else 0,
            area_sqm=area,
            rooms=a.get("rooms"),
            floor=a.get("floor"),
            address=_db_text(f'{a.get("city", "")} {a.get("address", "")}', 500),
            # Candidates are not report analogues until the evaluator chooses
            # three to five listings on the review screen.
            rank=None,
            is_selected=False,
            raw_data=a,
            screenshot_path=a.get("screenshot_path"),
        )
        if len(str(a.get("title") or "")) > 500:
            print(f"Analog title shortened for database: source={analog.source}, url={analog.url}", flush=True)
        db.add(analog)
        saved.append(analog)
        
        # Capture is intentionally deferred until /select-analogs.  This
        # avoids spending proxy/browser requests on listings the evaluator
        # does not ultimately use.

    screenshots_created = 0
    # No screenshots are made for the candidate pool.
    print(
        f"Comparable candidate pool prepared: report={report.id}, candidates={len(saved)}, "
        f"screenshots_requested={screenshots_requested}",
        flush=True,
    )
    
    report.recommended_value = statistics["recommended_value"]
    report.estimated_value = report.recommended_value
    report.selected_value = report.recommended_value
    report.range_min = statistics["range_min"]
    report.range_max = statistics["range_max"]
    report.benchmark_fdmu = e_certificate_value or report.recommended_value
    report.value_selection_mode = ValueSelectionMode.AUTOMATIC
    
    report.status = ReportStatus.ANALOGS_SEARCH
    await db.flush()
    
    return {
        "report_id": str(report.id),
        "analogs_found": len(saved),
        "selection_required": True,
        "low_candidate_pool": low_candidate_pool,
        "minimum_selectable": min(3, len(saved)),
        "maximum_selectable": min(5, len(saved)),
        "low_candidate_pool_warning": (
            f"Знайдено лише {len(saved)} зіставних об'єктів у цьому сегменті ринку "
            "(менше стандартних трьох). Звіт можна сформувати з наявною кількістю, "
            "але це варто зазначити в обґрунтуванні як ознаку тонкого ринку."
        ) if low_candidate_pool else None,
        "estimated_value": report.estimated_value,
        "range_min": report.range_min,
        "range_max": report.range_max,
        "recommended_value": report.recommended_value,
        "screenshots_requested": screenshots_requested,
        "screenshots_created": screenshots_created,
        "search_journal": search_journal,
        "statistics": statistics,
        "preview_analogs": [
            {"id": str(item.id), "number": index,
             "source": str(item.source or "").upper(), "rooms": item.rooms,
             "area_sqm": item.area_sqm, "floor": item.floor,
             "price_uah": item.price_uah, "price_per_sqm": item.price_per_sqm,
             "price_segment": (item.raw_data or {}).get("price_segment"),
             # Locality for the "Місце" column on the review screen.
             #
             # The Analog table has no city column -- the value is folded into
             # `address` when the row is written -- so read it back out of
             # raw_data, which holds the untouched provider payload. DIM.RIA
             # returns "city" and "district" directly; OLX has neither, so
             # fall back to the town name embedded in its listing URL
             # (.../realty-prodaja-dom-kosachevka-34739006.html) before giving
             # up. Without this the column rendered "—" for every row.
             "city": _analog_locality(item)}
            for index, item in enumerate(saved, start=1)
        ],
    }


@reports_router.post("/{report_id}/select-analogs")
async def select_analogs(
    report_id: str,
    req: SelectAnalogsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record an appraiser's final comparable listings and capture only those.

    Normally 3-5, matching the standard candidate pool. If the honest market
    sample itself has fewer than three comparable cards (thin local market),
    the appraiser must be able to proceed with what genuinely exists rather
    than hit a selection screen that can never be confirmed.
    """
    requested_ids = list(dict.fromkeys(str(value) for value in req.analog_ids if value))

    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")

    result = await db.execute(select(Analog).where(Analog.report_id == report.id))
    candidates = list(result.scalars().all())
    by_id = {str(item.id): item for item in candidates}
    if any(item_id not in by_id for item_id in requested_ids):
        raise HTTPException(400, "Частину аналогів не знайдено у поточній вибірці")

    minimum_required = min(3, len(candidates))
    maximum_allowed = min(5, len(candidates))
    if not minimum_required:
        raise HTTPException(400, "Немає жодного кандидата для вибору. Повторіть пошук аналогів.")
    if not minimum_required <= len(requested_ids) <= maximum_allowed:
        if len(candidates) < 3:
            raise HTTPException(
                400,
                f"У цьому пошуку знайдено лише {len(candidates)} зіставних об'єктів. "
                f"Оберіть усі {len(candidates)}, щоб продовжити (тонкий ринок).",
            )
        raise HTTPException(400, "Оберіть від 3 до 5 аналогів для формування звіту")

    for candidate in candidates:
        candidate.is_selected = False
        candidate.rank = None
        candidate.screenshot_path = None
        candidate.screenshot_verified = False
    selected = [by_id[item_id] for item_id in requested_ids]
    for rank, candidate in enumerate(selected, start=1):
        candidate.is_selected = True
        candidate.rank = rank

    from app.services.valuation_analytics import statistics_for_selected
    options = report.report_options or {}
    subject = {
        "area_sqm": report.area_sqm,
        "rooms": report.rooms,
        "floor": report.floor,
        "total_floors": report.total_floors,
        "year_built": report.year_built,
    }
    statistics = statistics_for_selected(
        [dict(item.raw_data or {}) for item in selected], subject
    )
    if not statistics.get("recommended_value"):
        raise HTTPException(400, "Недостатньо цінових даних у вибраних аналогах")
    e_certificate_value = float(options.get("e_certificate_value") or 0)
    statistics = _apply_report_valuation(report, statistics, options, e_certificate_value)
    report.valuation_statistics = statistics
    report.recommended_value = statistics["recommended_value"]
    report.estimated_value = report.recommended_value
    report.selected_value = report.recommended_value
    report.range_min = statistics["range_min"]
    report.range_max = statistics["range_max"]
    report.benchmark_fdmu = e_certificate_value or report.recommended_value
    report.value_selection_mode = ValueSelectionMode.AUTOMATIC

    # Selecting 3-5 listings must be instant. Browser screenshots are made
    # only by the final /generate request, after this professional choice.
    screenshots_requested = _screenshots_enabled(options)
    screenshots_created = 0

    journal = dict(report.search_journal or {})
    journal["appraiser_selection"] = {
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected_ids": requested_ids,
        "screenshots_requested": screenshots_requested,
        "screenshots_created": screenshots_created,
        "screenshots_deferred_until_generation": screenshots_requested,
        "selected_at": datetime.utcnow().isoformat(),
    }
    report.search_journal = journal
    report.status = ReportStatus.CALCULATING
    db.add(ActivityLog(user_id=user.id, action="select_analogs", details={
        "report_id": str(report.id), "candidate_count": len(candidates),
        "selected_count": len(selected), "screenshots_created": screenshots_created,
        "screenshots_deferred_until_generation": screenshots_requested,
    }))
    await db.flush()
    return {
        "report_id": str(report.id),
        "analogs_found": len(selected),
        "candidate_count": len(candidates),
        "estimated_value": report.estimated_value,
        "range_min": report.range_min,
        "range_max": report.range_max,
        "recommended_value": report.recommended_value,
        "screenshots_requested": screenshots_requested,
        "screenshots_created": screenshots_created,
        "screenshots_deferred": screenshots_requested,
        "statistics": statistics,
        "preview_analogs": [
            {"id": str(item.id), "number": item.rank, "source": str(item.source or "").upper(),
             "rooms": item.rooms, "area_sqm": item.area_sqm, "floor": item.floor,
             "price_uah": item.price_uah, "price_per_sqm": item.price_per_sqm}
            for item in selected
        ],
    }


@reports_router.post("/{report_id}/adjust")
async def adjust_value(
    report_id: str,
    req: AdjustValueRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Коригування вартості бігунком (±25% від бенчмарку)"""
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    
    if abs(req.adjustment_percent) > 25:
        raise HTTPException(400, "Коригування не може перевищувати ±25% від бенчмарку ФДМУ")
    
    if report.benchmark_fdmu:
        report.estimated_value = round(report.benchmark_fdmu * (1 + req.adjustment_percent / 100))
        report.adjustment_percent = req.adjustment_percent
    
    return {"estimated_value": report.estimated_value}


@reports_router.post("/{report_id}/select-value")
async def select_value(report_id: str, req: SelectValueRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Select a value without suggesting artificial under/over-valuation."""
    result = await db.execute(select(Report).where(Report.id == report_id, Report.user_id == user.id))
    report = result.scalar_one_or_none()
    if not report or report.range_min is None or report.range_max is None:
        raise HTTPException(400, "Спочатку виконайте аналіз аналогів")
    try:
        mode = ValueSelectionMode(req.mode)
    except ValueError:
        raise HTTPException(400, "Невідомий режим вибору вартості")
    outside = req.value < report.range_min or req.value > report.range_max
    differs = req.value != report.recommended_value
    if mode == ValueSelectionMode.AUTOMATIC and differs:
        raise HTTPException(400, "Автоматичний режим приймає рекомендовану вартість")
    if mode == ValueSelectionMode.PROFESSIONAL and outside:
        raise HTTPException(400, "Професійний режим дозволяє суму лише в рекомендованому діапазоні")
    # If an e-certificate is used, its statutory working corridor is a hard
    # boundary in every UI mode.  The application must not offer an "expert"
    # route that silently bypasses the ±25% limit.
    if report.benchmark_fdmu and outside:
        raise HTTPException(400, "Сума виходить за межі робочого коридору ±25% від е-довідки; сформувати звіт із таким значенням неможливо")
    if (outside or differs) and not (req.reason or "").strip():
        raise HTTPException(400, "Для відхилення від рекомендованої вартості потрібне обґрунтування")
    previous = report.selected_value or report.estimated_value
    report.selected_value = round(req.value)
    report.estimated_value = report.selected_value
    report.value_selection_mode = mode
    report.value_deviation_reason = (req.reason or "").strip() or None
    db.add(ValueChangeLog(report_id=report.id, user_id=user.id, previous_value=previous,
                          selected_value=report.selected_value, selection_mode=mode, reason=report.value_deviation_reason))
    db.add(ActivityLog(user_id=user.id, action="select_value", details={"report_id": str(report.id), "mode": mode.value, "outside_range": outside}))
    return {"selected_value": report.selected_value, "recommended_value": report.recommended_value,
            "range_min": report.range_min, "range_max": report.range_max, "warning": outside}


@reports_router.post("/{report_id}/options")
async def set_report_options(report_id: str, req: ReportOptionsRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if req.format not in {"table", "table_with_screenshots", "eoselia"}:
        raise HTTPException(400, "Непідтримуваний формат звіту")
    if req.report_package is not None and req.report_package not in {"basic", "evidence", "expert"}:
        raise HTTPException(400, "Невідомий пакет звіту")
    result = await db.execute(select(Report).where(Report.id == report_id, Report.user_id == user.id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    if req.trade_adjustment_percent < 0 or req.trade_adjustment_percent > 15:
        raise HTTPException(400, "Коригування на торг може бути від 0 до 15%")
    if req.trade_adjustment_percent and not (req.trade_adjustment_reason or "").strip():
        raise HTTPException(400, "Для коригування на торг потрібне обґрунтування оцінювача")
    if req.e_certificate_value is not None and req.e_certificate_value <= 0:
        raise HTTPException(400, "Сума з е-довідки має бути більшою за нуль")
    if req.e_certificate_manual_confirmed and not req.e_certificate_value:
        raise HTTPException(400, "Спочатку введіть суму для ручного підтвердження")
    if req.search_price_band not in {"lower", "center", "upper", "market"}:
        raise HTTPException(400, "Невідомий орієнтир цін для пошуку аналогів")
    if req.search_price_band != "market" and not req.e_certificate_value:
        raise HTTPException(400, "Для цінового орієнтира спочатку введіть суму з е-довідки")
    manual_city = (req.manual_city or "").strip()
    if len(manual_city) > 120:
        raise HTTPException(400, "Назва міста або населеного пункту занадто довга")
    valuation_date = (req.valuation_date or "").strip()
    if valuation_date:
        try:
            datetime.strptime(valuation_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Дата оцінки має бути у форматі РРРР-ММ-ДД")
    # A package is the source of truth for new reports.  Keep the derived
    # flags because report generation and older drafts use them directly.
    legacy_package = "expert" if req.full_expert_mode else ("evidence" if (req.include_screenshots or req.format == "table_with_screenshots") else "basic")
    package_name = req.report_package or legacy_package
    package_flags = {
        "basic": {"include_screenshots": False, "full_expert_mode": False},
        "evidence": {"include_screenshots": True, "full_expert_mode": False},
        "expert": {"include_screenshots": True, "full_expert_mode": True},
    }[package_name]
    report.report_options = {
        "report_package": package_name,
        "include_screenshots": package_flags["include_screenshots"],
        "include_olx": req.include_olx,
        "include_appraiser_documents": req.include_appraiser_documents,
        "trade_adjustment_percent": req.trade_adjustment_percent,
        "trade_adjustment_reason": (req.trade_adjustment_reason or "").strip() or None,
        "e_certificate_value": req.e_certificate_value,
        "e_certificate_manual_confirmed": bool(req.e_certificate_manual_confirmed),
        "search_price_band": req.search_price_band,
        "full_expert_mode": package_flags["full_expert_mode"],
        "manual_city": manual_city or None,
        "valuation_date": valuation_date or None,
        "format": req.format,
    }
    # Flush and log the exact saved value.  The browser can now detect a
    # stale front-end/backend mismatch instead of silently producing a report
    # without screenshots after the appraiser ticked the checkbox.
    await db.flush()
    db.add(ActivityLog(user_id=user.id, action="set_report_options", details={
        "report_id": str(report.id), **report.report_options,
    }))
    print(f"Report options saved: report={report.id}, options={report.report_options}", flush=True)
    return report.report_options


@reports_router.post("/{report_id}/object-photos")
async def upload_object_photos(report_id: str, files: list[UploadFile] = File(default=[]), listing_url: Optional[str] = Form(None), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """The only new UI-facing upload point: object photos and optional OLX/DIM.RIA link."""
    result = await db.execute(select(Report).where(Report.id == report_id, Report.user_id == user.id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    target = Path(settings.object_photos_dir) / str(user.id) / str(report.id)
    target.mkdir(parents=True, exist_ok=True)
    stored = list(report.object_photo_files or [])
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        path = target / f"{uuid.uuid4()}{suffix}"
        path.write_bytes(await file.read())
        stored.append(str(path))
    report.object_photo_files = stored
    if listing_url:
        if not any(host in listing_url.lower() for host in ("olx.", "dom.ria.", "dim.ria.")):
            raise HTTPException(400, "Дозволені лише посилання OLX або DIM.RIA")
        report.object_listing_url = listing_url.strip()
    return {"photos": len(stored), "listing_url": report.object_listing_url,
            "warning": None if stored or report.object_listing_url else "Фотографії об'єкта відсутні"}


async def _upload_report_evidence(report: Report, user: User, files: list[UploadFile], kind: str) -> list[str]:
    """Store appraiser-supplied evidence only; no state-system retrieval."""
    target = Path(settings.upload_dir) / str(user.id) / str(report.id) / kind
    target.mkdir(parents=True, exist_ok=True)
    allowed = {".pdf", ".jpg", ".jpeg", ".png"}
    if kind == "e_certificate":
        allowed.add(".docx")
        allowed.add(".doc")
    stored = list(getattr(report, f"{kind}_files") or [])
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in allowed:
            continue
        body = await file.read()
        if not body or len(body) > 25 * 1024 * 1024:
            continue
        path = target / f"{uuid.uuid4()}{suffix}"
        path.write_bytes(body)
        stored.append(str(path))
    setattr(report, f"{kind}_files", stored)
    return stored


def _parse_e_certificate_value(file_paths: list[str]) -> float | None:
    """Extract 'Оціночна вартість об'єкта оцінки' from an e-certificate PDF or DOCX.

    The e-certificate (Додаток 2 ФДМУ) has a fixed structure with line 20:
    '20. Оціночна вартість об'єкта оцінки, грн' followed by the value.
    """
    import re
    for path in file_paths:
        lower = path.lower()
        text = ""
        try:
            if lower.endswith(".pdf"):
                import subprocess
                result = subprocess.run(
                    ["pdftotext", "-layout", path, "-"],
                    capture_output=True, text=True, timeout=10,
                )
                text = result.stdout or ""
                if not text.strip():
                    result = subprocess.run(
                        ["pdftotext", path, "-"],
                        capture_output=True, text=True, timeout=10,
                    )
                    text = result.stdout or ""
            elif lower.endswith(".docx"):
                try:
                    from docx import Document
                    doc = Document(path)
                    text = "\n".join(p.text for p in doc.paragraphs)
                    # Also check tables (ФДМУ form may use tables)
                    for table in doc.tables:
                        for row in table.rows:
                            text += "\n" + " ".join(cell.text for cell in row.cells)
                except Exception as e:
                    print(f"E-certificate DOCX parse error: {e}")
                    continue
            else:
                continue

            if not text.strip():
                continue

            # Pattern: "20. Оціночна вартість об'єкта оцінки, грн"
            # followed by a number like 203318.85
            match = re.search(
                r"20\.\s*[Оо]ціночна\s+вартість\s+об.єкта\s+оцінки.*?(\d[\d\s]*[\.,]\d{1,2})",
                text, re.IGNORECASE | re.DOTALL,
            )
            if match:
                raw = match.group(1).replace(" ", "").replace(",", ".")
                value = float(raw)
                if value > 0:
                    return round(value, 2)
            # Broader fallback: any "оціночна вартість" followed by a number
            match = re.search(
                r"[Оо]ціночна\s+вартість.*?(\d{4,}[\.,]?\d{0,2})",
                text, re.IGNORECASE | re.DOTALL,
            )
            if match:
                raw = match.group(1).replace(" ", "").replace(",", ".")
                value = float(raw)
                if value > 100:
                    return round(value, 2)
        except Exception as e:
            print(f"E-certificate parse error: {e}")
    return None


@reports_router.post("/{report_id}/e-certificate")
async def upload_e_certificate(report_id: str, files: list[UploadFile] = File(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Report).where(Report.id == report_id, Report.user_id == user.id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    stored = await _upload_report_evidence(report, user, files, "e_certificate")
    if not stored:
        raise HTTPException(400, "Додайте PDF, JPG, PNG або DOCX е-довідки до 25 МБ")
    db.add(ActivityLog(user_id=user.id, action="e_certificate_uploaded", details={"report_id": str(report.id), "count": len(stored)}))
    # Try to parse the e-certificate value from the uploaded PDF
    parsed_value = _parse_e_certificate_value(stored)
    response = {"files": len(stored)}
    if parsed_value:
        response["parsed_value"] = parsed_value
        print(f"E-certificate parsed value: {parsed_value} грн from {stored[0]}")
    return response


@reports_router.post("/{report_id}/location-map")
async def upload_location_map(report_id: str, files: list[UploadFile] = File(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Report).where(Report.id == report_id, Report.user_id == user.id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    stored = await _upload_report_evidence(report, user, files, "location_map")
    if not stored:
        raise HTTPException(400, "Додайте PDF, JPG або PNG карти до 25 МБ")
    db.add(ActivityLog(user_id=user.id, action="location_map_uploaded", details={"report_id": str(report.id), "count": len(stored)}))
    return {"files": len(stored)}


@reports_router.post("/{report_id}/generate")
async def generate_report(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Генерація Word і PDF документів"""
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    
    started_at = perf_counter()
    report.status = ReportStatus.GENERATING
    logger.info("report=%s generation started package=%s", report_id, (report.report_options or {}).get("report_package"))
    
    # Аналоги
    analogs_result = await db.execute(
        select(Analog).where(Analog.report_id == report.id, Analog.is_selected == True)
    )
    analogs = analogs_result.scalars().all()
    if len(analogs) < 3:
        raise HTTPException(400, "Спочатку оберіть від 3 до 5 аналогів для звіту")
    
    # Build all three editable Word documents once.  Downloads only return
    # already prepared files and must never start a second heavy generation.

    # Генерація Word
    stage_started = perf_counter()
    word_path = await generate_word_compact(report, user, analogs)
    report.word_path = word_path
    logger.info("report=%s stage=compact_word seconds=%.2f", report_id, perf_counter() - stage_started)
    stage_started = perf_counter()
    conclusion_path = await generate_word_conclusion(report, user)
    logger.info("report=%s stage=conclusion_word seconds=%.2f", report_id, perf_counter() - stage_started)

    options = report.report_options or {}
    screenshots_requested = _screenshots_enabled(options)
    stage_started = perf_counter()
    screenshots_created = (
        await _capture_selected_analog_screenshots(report, analogs)
        if screenshots_requested else 0
    )
    logger.info("report=%s stage=screenshots requested=%s created=%s seconds=%.2f", report_id, screenshots_requested, screenshots_created, perf_counter() - stage_started)
    print(f"Screenshots: report={report_id} requested={screenshots_requested} "
          f"created={screenshots_created} seconds={perf_counter() - stage_started:.1f}")
    statistics = dict(report.valuation_statistics or {})
    if _expert_enabled(options):
        stage_started = perf_counter()
        evidence = await analyse_property_evidence(report)
        logger.info("report=%s stage=photo_evidence reviewed=%s features=%s seconds=%.2f", report_id, evidence.get("photos_reviewed", 0), len(evidence.get("visible_features") or []) + len(evidence.get("condition_observations") or []), perf_counter() - stage_started)
        stage_started = perf_counter()
        expert_text = await generate_expert_report_text(
            report.ocr_raw or {}, [item.raw_data or {} for item in analogs], evidence,
            price_position=_price_position_for_report(report),
        )
        logger.info("report=%s stage=expert_text seconds=%.2f", report_id, perf_counter() - stage_started)
        statistics["expert_full"] = {
            "evidence": evidence,
            "text": expert_text,
        }
    else:
        statistics.pop("expert_full", None)
    report.valuation_statistics = statistics
    stage_started = perf_counter()
    full_word_path = await generate_full_word_package(
        report, user, analogs, include_screenshots=screenshots_requested,
    )
    logger.info("report=%s stage=full_word seconds=%.2f total_seconds=%.2f", report_id, perf_counter() - stage_started, perf_counter() - started_at)
    report.status = ReportStatus.READY
    report.completed_at = datetime.utcnow()
    current_plan = user.plan.value if hasattr(user.plan, "value") else str(user.plan or "free")
    if not user.is_admin and current_plan == SubscriptionPlan.FREE.value:
        user.free_reports_left = max(0, (user.free_reports_left or 0) - 1)
    elif not user.is_admin:
        user.reports_this_month = (user.reports_this_month or 0) + 1
    db.add(ActivityLog(user_id=user.id, action="generate_word_set", details={
        "report_id": str(report.id),
        "screenshots_requested": screenshots_requested,
        "screenshots_created": screenshots_created,
    }))
    return {
        "status": "ready",
        "word": word_path,
        "conclusion_word": conclusion_path,
        "full_word": full_word_path,
        "full_word_ready": True,
        "screenshots_requested": screenshots_requested,
        "screenshots_created": screenshots_created,
    }


@reports_router.post("/{report_id}/generate-full-word")
async def generate_full_word_package_endpoint(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create the heavy editable DOCX package only on explicit request."""
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    if not report.word_path or not os.path.exists(report.word_path):
        raise HTTPException(400, "Спочатку сформуйте Word-звіт")

    analogs_result = await db.execute(
        select(Analog).where(Analog.report_id == report.id, Analog.is_selected == True)
    )
    analogs = analogs_result.scalars().all()
    if len(analogs) < 3:
        raise HTTPException(400, "Спочатку оберіть від 3 до 5 аналогів для звіту")

    report.status = ReportStatus.GENERATING
    options = report.report_options or {}
    screenshots_requested = _screenshots_enabled(options)
    screenshots_created = (
        await _capture_selected_analog_screenshots(report, analogs)
        if screenshots_requested else 0
    )

    statistics = dict(report.valuation_statistics or {})
    if _expert_enabled(options):
        evidence = await analyse_property_evidence(report)
        expert_text = await generate_expert_report_text(
            report.ocr_raw or {}, [item.raw_data or {} for item in analogs], evidence,
            price_position=_price_position_for_report(report),
        )
        statistics["expert_full"] = {"evidence": evidence, "text": expert_text}
    else:
        statistics.pop("expert_full", None)
    report.valuation_statistics = statistics

    full_word_path = await generate_full_word_package(
        report, user, analogs, include_screenshots=screenshots_requested,
    )
    report.status = ReportStatus.READY
    report.completed_at = datetime.utcnow()
    db.add(ActivityLog(user_id=user.id, action="generate_full_word_package", details={
        "report_id": str(report.id), "screenshots_created": screenshots_created,
    }))
    return {
        "status": "full_word_ready",
        "full_word": full_word_path,
        "screenshots_requested": screenshots_requested,
        "screenshots_created": screenshots_created,
    }


@reports_router.post("/{report_id}/generate-pdf")
async def generate_pdf_package(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Build the slower PDF package only when it is explicitly requested."""
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    if not report.word_path or not os.path.exists(report.word_path):
        raise HTTPException(400, "Спочатку сформуйте Word-чернетку")

    analogs_result = await db.execute(
        select(Analog).where(Analog.report_id == report.id, Analog.is_selected == True)
    )
    analogs = analogs_result.scalars().all()
    if len(analogs) < 3:
        raise HTTPException(400, "Спочатку оберіть від 3 до 5 аналогів для звіту")

    report.status = ReportStatus.GENERATING
    options = report.report_options or {}
    screenshots_requested = _screenshots_enabled(options)
    screenshots_created = (
        await _capture_selected_analog_screenshots(report, analogs)
        if screenshots_requested else 0
    )

    statistics = dict(report.valuation_statistics or {})
    if _expert_enabled(options):
        evidence = await analyse_property_evidence(report)
        expert_text = await generate_expert_report_text(
            report.ocr_raw or {}, [item.raw_data or {} for item in analogs], evidence,
            price_position=_price_position_for_report(report),
        )
        statistics["expert_full"] = {"evidence": evidence, "text": expert_text}
    else:
        statistics.pop("expert_full", None)
    report.valuation_statistics = statistics

    report.pdf_conclusion_path = await generate_pdf_conclusion(report, user)
    report.pdf_full_path = await generate_full_pdf_compact(
        report, user, analogs, include_screenshots=screenshots_requested,
    )
    report.status = ReportStatus.READY
    report.completed_at = datetime.utcnow()
    db.add(ActivityLog(user_id=user.id, action="generate_pdf_package", details={
        "report_id": str(report.id), "screenshots_created": screenshots_created,
    }))
    return {
        "status": "pdf_ready", "pdf_conclusion": report.pdf_conclusion_path,
        "pdf_full": report.pdf_full_path,
        "screenshots_requested": screenshots_requested,
        "screenshots_created": screenshots_created,
    }


@reports_router.get("/{report_id}/download/{format}")
async def download_file(
    report_id: str,
    format: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Завантаження файлу звіту"""
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")
    
    report_dir = os.path.dirname(report.word_path) if report.word_path else ""
    paths = {
        "word": report.word_path,
        "conclusion-word": os.path.join(report_dir, "conclusion.docx") if report_dir else None,
        "full-word": os.path.join(report_dir, "full_report.docx") if report_dir else None,
        # Legacy PDF downloads remain available by direct API URL only.
        "pdf": report.pdf_conclusion_path,
        "full": report.pdf_full_path,
    }
    path = paths.get(format)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "Файл не знайдено")
    
    db.add(ActivityLog(user_id=user.id, action="download", details={"report_id": str(report.id), "format": format}))
    
    media_types = {
        "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "conclusion-word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "full-word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
        "full": "application/pdf",
    }
    return FileResponse(path, media_type=media_types.get(format, "application/octet-stream"), filename=os.path.basename(path))


def _report_next_step(status, ready: bool) -> str:
    """Where the 'Продовжити' button should take an unfinished report.

    Everything the client entered (uploaded documents, OCR data, address,
    analogs, selection, captured screenshots) is already persisted in the DB /
    on disk, so re-opening at the right stage loses nothing — and re-running
    generation reuses existing screenshots, so the provider quota is not spent
    again. Shared by GET /my and GET /{report_id}.
    """
    s = status.value if hasattr(status, "value") else str(status)
    if ready or s == "ready":
        return "download"              # готово — просто завантажити
    return {
        "uploading": "upload",             # дозавантажити документи
        "ocr_processing": "confirm_data",  # підтвердити розпізнані дані
        "ocr_failed": "confirm_data",      # ввести дані вручну
        "analogs_search": "select_analogs",  # обрати аналоги (вже знайдені)
        "calculating": "select_value",     # обрати вартість
        "generating": "generate",          # продовжити генерацію (reuse скрінів)
        "error": "generate",               # повторити
    }.get(s, "confirm_data")


@reports_router.get("/my")
async def my_reports(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список звітів поточного користувача"""
    result = await db.execute(
        select(Report).where(Report.user_id == user.id).order_by(Report.created_at.desc()).limit(50)
    )
    reports = result.scalars().all()
    return [
        {
            "id": str(r.id), "address": r.address,
            "deal_type": r.deal_type.value if hasattr(r.deal_type, "value") else r.deal_type,
            "object_type": r.object_type.value if hasattr(r.object_type, "value") else r.object_type,
            "area_sqm": r.area_sqm,
            "status": r.status.value if hasattr(r.status, "value") else r.status,
            "estimated_value": r.estimated_value,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "has_word": bool(r.word_path),
            "has_pdf": bool(r.pdf_conclusion_path),
            "has_full": bool(r.pdf_full_path),
            # True while the report is not finished — the frontend shows
            # "Продовжити створення звіту" instead of a download button.
            "is_resumable": (r.status.value if hasattr(r.status, "value") else r.status) != "ready",
            "next_step": _report_next_step(r.status, bool(r.word_path or r.pdf_full_path)),
        }
        for r in reports
    ]


@reports_router.get("/{report_id}")
async def get_report(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Full current state of one report, for resuming an unfinished one.

    Returns everything the frontend needs to re-open a report at the stage the
    client left it — including the analog candidates ALREADY found and their
    selection — so "Продовжити" never re-runs the paid analog search or
    re-captures screenshots. Defined after /my so the literal path still wins.
    """
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user.id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Звіт не знайдено")

    analogs_result = await db.execute(
        select(Analog).where(Analog.report_id == report.id)
    )
    candidates = list(analogs_result.scalars().all())
    selected_sorted = sorted(
        (c for c in candidates if c.is_selected),
        key=lambda c: (c.rank if c.rank is not None else 1_000_000),
    )

    def _enum(v):
        return v.value if hasattr(v, "value") else v

    status = _enum(report.status)
    return {
        "id": str(report.id),
        "status": status,
        "is_resumable": status != "ready",
        "next_step": _report_next_step(report.status, bool(report.word_path or report.pdf_full_path)),
        "address": report.address,
        "object_type": _enum(report.object_type),
        "deal_type": _enum(report.deal_type),
        "eval_mode": _enum(report.eval_mode),
        "area_sqm": report.area_sqm,
        "rooms": report.rooms,
        "floor": report.floor,
        "total_floors": report.total_floors,
        "year_built": report.year_built,
        "cadastral_number": report.cadastral_number,
        "estimated_value": report.estimated_value,
        "range_min": report.range_min,
        "range_max": report.range_max,
        "recommended_value": report.recommended_value,
        "report_options": report.report_options or {},
        "has_ocr": bool(report.ocr_raw),
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "has_word": bool(report.word_path),
        "has_pdf": bool(report.pdf_conclusion_path),
        "has_full": bool(report.pdf_full_path),
        "candidate_count": len(candidates),
        "selected_count": sum(1 for c in candidates if c.is_selected),
        # Same shape as find-analogs' preview_analogs, plus resume state, so the
        # review screen can be rebuilt without a new search.
        "analogs": [
            {
                "id": str(c.id),
                "source": str(c.source or "").upper(),
                "url": c.url,
                "rooms": c.rooms,
                "area_sqm": c.area_sqm,
                "floor": c.floor,
                "price_uah": c.price_uah,
                "price_per_sqm": c.price_per_sqm,
                "price_segment": (c.raw_data or {}).get("price_segment"),
                "city": _analog_locality(c),
                "is_selected": bool(c.is_selected),
                "rank": c.rank,
                "has_screenshot": bool(c.screenshot_path and os.path.exists(c.screenshot_path)),
            }
            for c in (selected_sorted + [c for c in candidates if not c.is_selected])
        ],
    }


class BulkDeleteReportsRequest(BaseModel):
    report_ids: list[str]


@reports_router.post("/bulk-delete")
async def bulk_delete_reports(
    req: BulkDeleteReportsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete selected reports: DB row, generated files and scans.

    This is a real delete, not a soft/hide flag — the checkbox in "Мої звіти"
    is expected to actually free the file storage, not just unlist it. It
    never touches free_reports_left / reports_this_month: the generation
    quota was already spent when the report was created, and deleting the
    output afterwards must not refund it (per product decision — otherwise
    generate-then-delete would be a free way around the plan limit).
    """
    if not req.report_ids:
        return {"deleted": 0, "not_found": 0}
    ids: list[uuid.UUID] = []
    for raw_id in req.report_ids:
        try:
            ids.append(uuid.UUID(str(raw_id)))
        except ValueError:
            continue
    if not ids:
        return {"deleted": 0, "not_found": len(req.report_ids)}

    result = await db.execute(
        select(Report).where(Report.id.in_(ids), Report.user_id == user.id)
    )
    reports = result.scalars().all()
    found_ids = {report.id for report in reports}
    not_found = len(ids) - len(found_ids)

    def _remove_file(path):
        if not path:
            return
        try:
            candidate = Path(str(path))
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
        except Exception as error:
            print(f"Report file delete error: path={path}, error={error}")

    deleted_count = 0
    for report in reports:
        # Explicitly tracked single-file fields.
        for attr in ("word_path", "pdf_conclusion_path", "pdf_full_path"):
            _remove_file(getattr(report, attr, None))
        # List-valued upload fields (original scans, object photos, e-довідка
        # files, location maps) — each is a JSON array of paths.
        for attr in ("upload_files", "object_photo_files", "e_certificate_files", "location_map_files"):
            for path in (getattr(report, attr, None) or []):
                _remove_file(path)
        # Whole per-report directories: generated docx/pdf variants live
        # under generated_dir/<user_id>/<report_id>/, and analog evidence
        # screenshots live under screenshots_dir/<report_id>/. Removing the
        # directory catches any file not individually tracked above.
        for directory in (
            Path(settings.generated_dir) / str(report.user_id) / str(report.id),
            Path(settings.screenshots_dir) / str(report.id),
        ):
            try:
                if directory.is_dir():
                    shutil.rmtree(directory, ignore_errors=True)
            except Exception as error:
                print(f"Report directory delete error: path={directory}, error={error}")
        # Child rows that Postgres will not remove on its own.
        #
        # Analog has cascade="all, delete-orphan" on the relationship, so the
        # ORM clears it. ValueChangeLog does not: its foreign key has no
        # cascade, so the DELETE was rejected outright --
        # "violates foreign key constraint value_change_log_report_id_fkey"
        # -- and the whole request failed with a 500 while the files on disk
        # had already been removed. Clearing the log first keeps the delete
        # inside the same transaction as the rest.
        #
        # If another table ever gains a report_id foreign key, it needs the
        # same line here or deletion breaks the same way.
        await db.execute(delete(ValueChangeLog).where(ValueChangeLog.report_id == report.id))
        await db.delete(report)  # cascades to Analog rows (cascade="all, delete-orphan")
        deleted_count += 1

    db.add(ActivityLog(
        user_id=user.id, action="delete_reports",
        details={"report_ids": [str(i) for i in found_ids], "count": deleted_count},
    ))
    await db.commit()
    return {"deleted": deleted_count, "not_found": not_found}


# ========== ADMIN ==========

@partners_router.post("/apply")
async def apply_partner(req: PartnerApplicationRequest, db: AsyncSession = Depends(get_db)):
    """Store an agency/agent request for review in the protected admin panel."""
    referral = AgencyReferral(
        agency_name=req.agency_name.strip(), contact_person=req.contact_person.strip(),
        contact_email=str(req.contact_email), contact_phone=req.contact_phone.strip(),
        reports_per_month=req.reports_per_month, team_size=req.team_size,
        message=(req.message or "").strip() or None, status="pending",
        referral_code="AG-" + uuid.uuid4().hex[:8].upper(), partner_percent=10.0,
    )
    db.add(referral)
    return {"ok": True, "application_id": str(referral.id)}

# Human-readable stage labels for the admin "why isn't this report done"
# view. Kept in sync with ReportStatus in app/app/models/models.py.
ADMIN_REPORT_STATUS_LABELS = {
    ReportStatus.UPLOADING: "Завантаження файлів",
    ReportStatus.OCR_PROCESSING: "Розпізнавання документів (OCR)",
    ReportStatus.OCR_FAILED: "OCR не розпізнав документи",
    ReportStatus.ANALOGS_SEARCH: "Пошук аналогів",
    ReportStatus.CALCULATING: "Розрахунок вартості",
    ReportStatus.GENERATING: "Генерація документів",
    ReportStatus.READY: "Готово",
    ReportStatus.ERROR: "Помилка",
}

# Ukrainian labels for the ActivityLog feed shown in the admin client
# drill-down. An action missing here still displays (falls back to the raw
# action string), so a newly added ActivityLog call never breaks this view.
ADMIN_ACTIVITY_LABELS = {
    "register": "Реєстрація",
    "login": "Вхід у кабінет",
    "profile_updated": "Оновлення профілю",
    "profile_documents_uploaded": "Завантажено документи профілю",
    "profile_document_deleted": "Видалено документ профілю",
    "create_report": "Створено новий звіт",
    "object_type_mismatch": "Невідповідність типу об'єкта при завантаженні",
    "upload": "Завантажено документи об'єкта",
    "confirm_ocr_data": "Підтверджено дані після OCR",
    "select_analogs": "Обрано аналоги для звіту",
    "select_value": "Обрано підсумкову вартість",
    "set_report_options": "Налаштовано параметри звіту",
    "e_certificate_uploaded": "Завантажено е-довідку ФДМУ",
    "location_map_uploaded": "Завантажено карту розташування",
    "generate_word_set": "Згенеровано комплект Word",
    "generate_full_word_package": "Згенеровано повний Word-пакет",
    "generate_pdf_package": "Згенеровано PDF-пакет",
    "download": "Завантажено готовий файл",
    "delete_reports": "Видалено звіти",
}

# A report normally moves through its stages within minutes. Past this age
# without reaching READY/ERROR, it is very likely abandoned or stuck rather
# than "still in progress" — this threshold only affects the wording of the
# admin hint, never any user-facing behaviour.
ADMIN_STUCK_REPORT_HOURS = 2


def _ocr_quality_hint(report: Report) -> str:
    """Human-readable summary of what OCR actually found, built only from
    ocr_raw (missing_documents / missing_data / confidence) that ocr_service
    already computes on every upload. Empty string when there is nothing
    noteworthy to add (good confidence, nothing missing)."""
    ocr_raw = report.ocr_raw or {}
    parts = []
    missing_documents = ocr_raw.get("missing_documents") or []
    missing_data = ocr_raw.get("missing_data") or []
    confidence = ocr_raw.get("confidence")
    if missing_documents:
        parts.append("не знайдено серед завантажених документів: " + ", ".join(missing_documents))
    if missing_data:
        parts.append("не вдалося визначити дані: " + ", ".join(missing_data))
    if confidence is not None and confidence > 0 and confidence < 0.5:
        parts.append(f"низька впевненість розпізнавання ({confidence:.0%}) — варто перевірити якість сканів")
    if ocr_raw.get("_needs_review"):
        parts.append("є розбіжності між документами, позначені для ручної перевірки оцінювачем")
    return "; ".join(parts)


def _report_stall_reason(report: Report) -> Optional[str]:
    """Best-effort explanation of why a report is not READY yet.

    Uses only data already recorded on the report (status, error_message,
    upload_files count, ocr_raw, loaded analogs) — no extra instrumentation.
    Returns None once a report is READY, since there is nothing to explain.
    """
    documents_uploaded = len(report.upload_files or [])
    ocr_hint = _ocr_quality_hint(report)

    if report.status == ReportStatus.READY:
        return None

    if report.status == ReportStatus.ERROR:
        base = report.error_message or "Сталася помилка під час генерації (деталі не збережено в журналі)"
        return base + (f" ({ocr_hint})" if ocr_hint else "")

    if report.status == ReportStatus.OCR_FAILED:
        ocr_raw = report.ocr_raw or {}
        if ocr_raw.get("_object_type_mismatch"):
            detected = OBJECT_TYPE_LABELS.get(ocr_raw.get("_detected_object_type"), ocr_raw.get("_detected_object_type") or "інший об'єкт")
            selected = OBJECT_TYPE_LABELS.get(ocr_raw.get("_selected_object_type"), ocr_raw.get("_selected_object_type") or "інший тип")
            return (
                f"У документах розпізнано «{detected}», а на сайті обрано «{selected}» — "
                f"невідповідність типу об'єкта, пошук аналогів не запускався. "
                f"Завантажено документів: {documents_uploaded}."
            )
        if report.error_message:
            return (
                f"OCR не зміг обробити документи: {report.error_message}. "
                f"Завантажено документів: {documents_uploaded} — файли збережено, оцінювач може спробувати ще раз."
            )
        return (
            f"OCR не розпізнав завантажені документи. Завантажено документів: {documents_uploaded}"
            + (" — це підозріло мало, можливо, оцінювач завантажив не всі потрібні файли." if documents_uploaded <= 1 else ".")
            + (f" {ocr_hint}." if ocr_hint else "")
        )

    if report.status == ReportStatus.OCR_PROCESSING:
        return (
            f"Розпізнавання документів (OCR) розпочато, але не завершилось у межах запиту "
            f"(зазвичай це відбувається за секунди-хвилину). Завантажено документів: {documents_uploaded}. "
            "Найімовірніше, з'єднання з оцінювачем перервалось або перезапустився сервер під час обробки — "
            "оцінювачу варто спробувати завантажити документи ще раз."
        )

    age_hours = (
        (datetime.utcnow() - report.created_at).total_seconds() / 3600
        if report.created_at else 0
    )
    stale = age_hours > ADMIN_STUCK_REPORT_HOURS
    stale_suffix = " Із моменту створення минуло понад {:.0f} год без завершення — ймовірно, покинуто оцінювачем або стався збій.".format(age_hours) if stale else ""
    ocr_suffix = f" (OCR: {ocr_hint})" if ocr_hint else ""

    if report.status == ReportStatus.UPLOADING:
        return f"Оцінювач ще не завершив завантаження документів (поки що: {documents_uploaded})." + stale_suffix
    if report.status == ReportStatus.ANALOGS_SEARCH:
        analogs = list(report.analogs or [])
        if not analogs:
            return "Пошук аналогів не знайшов жодного об'єкта — можливо, майданчик тимчасово недоступний або критерії занадто вузькі." + ocr_suffix + stale_suffix
        return f"Аналоги знайдено ({len(analogs)}), але оцінювач ще не обрав фінальні 3–5 для звіту." + ocr_suffix + stale_suffix
    if report.status == ReportStatus.CALCULATING:
        return "Розрахунок вартості розпочато, але не завершено." + ocr_suffix + stale_suffix
    if report.status == ReportStatus.GENERATING:
        if stale:
            return "Генерація документів розпочалась, але не завершилась" + stale_suffix + " Перевірте логи ocinka-backend на сервері."
        return "Генерація документів ще триває."
    return "Невідома стадія." + stale_suffix


def _admin_report_detail(report: Report) -> dict:
    analogs = list(report.analogs or [])
    ocr_raw = report.ocr_raw or {}
    return {
        "id": str(report.id),
        "object_type": report.object_type.value if hasattr(report.object_type, "value") else report.object_type,
        "deal_type": report.deal_type.value if hasattr(report.deal_type, "value") else report.deal_type,
        "address": report.address,
        "status": report.status.value if hasattr(report.status, "value") else report.status,
        "status_label": ADMIN_REPORT_STATUS_LABELS.get(report.status, str(report.status)),
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "completed_at": report.completed_at.isoformat() if report.completed_at else None,
        "estimated_value": report.estimated_value,
        "error_message": report.error_message,
        "analogs_found": len(analogs),
        "analogs_selected": sum(1 for a in analogs if a.is_selected),
        "documents_uploaded": len(report.upload_files or []),
        "ocr_provider": report.ocr_provider,
        "ocr_confidence": ocr_raw.get("confidence"),
        "ocr_missing_documents": ocr_raw.get("missing_documents") or [],
        "ocr_missing_data": ocr_raw.get("missing_data") or [],
        "possible_reason": _report_stall_reason(report),
    }


# --- Повний бекап "в один клік" з адмін-панелі -------------------------------
# Ручний, натискається адміном за потреби; окреме автоматичне (cron) рішення
# на власному сховищі — на майбутнє. Це читає наявні дані й пакує їх у
# тимчасовий архів, нічого в БД чи на диску не змінюючи.
#
# .env і будь-які API-ключі СВІДОМО не включені: цей ендпоінт віддає файл
# через звичайний HTTP-запит під адмінським токеном, а не викликається
# локально на сервері (як окремий offsite-скрипт) — тримати секрети поза
# файлом, що можна скачати одним запитом, безпечніше.
ADMIN_BACKUP_APP_DIR = "/opt/ocinka"
ADMIN_BACKUP_SITE_DIR = "/var/www/ocinka.pro"
ADMIN_BACKUP_NGINX_CONF = "/etc/nginx/sites-available/ocinka.pro"
# Скріншоти аналогів (settings.screenshots_dir) навмисно НЕ включені —
# сервер сам чистить їх за settings.screenshot_retention_hours, архівної
# цінності самі файли не мають (їхній вміст і так потрапляє у фінальні
# Word/PDF як вставлені зображення — це не обходиться і не повинно).
# uploaded-documents — це саме те, що завантажує оцінювач на самому початку
# (техпаспорт, правовстановлюючий документ тощо, settings.upload_dir).
ADMIN_BACKUP_DATA_DIRS = {
    "uploaded-documents": getattr(settings, "upload_dir", None),
    "generated": getattr(settings, "generated_dir", None),
    "profile-documents": getattr(settings, "profile_documents_dir", None),
    "object-photos": getattr(settings, "object_photos_dir", None),
    "report-templates": getattr(settings, "report_templates_dir", None),
}


def _admin_dump_database() -> bytes:
    """pg_dump у custom-форматі (той самий, що вже використовується у
    ручних бекапах db-ocinka.dump). Пароль передається через PGPASSWORD,
    щоб не світити його в списку процесів."""
    parsed = urlparse(settings.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    cmd = [
        "pg_dump", "-Fc",
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "ocinka",
        (parsed.path or "/ocinka").lstrip("/"),
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[:1000] or "pg_dump завершився з помилкою")
    return result.stdout


def _admin_code_tar_filter(tarinfo: tarfile.TarInfo):
    """Виключає venv, __pycache__ і .env з архіву коду бекенду — не
    потрібні для відновлення даних, а .env містить API-ключі."""
    name = tarinfo.name
    if "/venv/" in name or name.endswith("/venv") or "__pycache__" in name:
        return None
    if name.endswith("/.env") or name.endswith(".env"):
        return None
    return tarinfo


@admin_router.get("/backup/download")
async def admin_download_backup(admin: User = Depends(get_admin_user)):
    """Бекап 'в один клік': дамп БД + файли звітів/документів (без
    скріншотів) + код бекенду й сайту + конфіг nginx, одним .tar.gz.
    Лише читає наявні дані; нічого в БД чи на диску не змінюється."""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    tmp_path = Path(tempfile.gettempdir()) / f"ocinka-backup-{timestamp}.tar.gz"
    dump_tmp_path = Path(tempfile.gettempdir()) / f"ocinka-db-{timestamp}.dump"
    try:
        with tarfile.open(tmp_path, "w:gz") as archive:
            # 1. База даних
            try:
                dump_tmp_path.write_bytes(_admin_dump_database())
                archive.add(dump_tmp_path, arcname="db/ocinka.dump")
            except Exception as error:
                note = f"pg_dump не вдався: {error}".encode("utf-8")
                info = tarfile.TarInfo(name="db/DUMP_FAILED.txt")
                info.size = len(note)
                archive.addfile(info, io.BytesIO(note))
            finally:
                dump_tmp_path.unlink(missing_ok=True)

            # 2. Файли звітів і документів (скріншоти виключено)
            for label, path_str in ADMIN_BACKUP_DATA_DIRS.items():
                if not path_str:
                    continue
                path = Path(path_str)
                if path.is_dir() and any(path.iterdir()):
                    archive.add(path, arcname=f"files/{label}")

            # 3. Код бекенду й сайту
            app_dir = Path(ADMIN_BACKUP_APP_DIR)
            if app_dir.is_dir():
                archive.add(app_dir, arcname="app-code/app", filter=_admin_code_tar_filter)
            site_dir = Path(ADMIN_BACKUP_SITE_DIR)
            if site_dir.is_dir():
                archive.add(site_dir, arcname="site-code/site")

            # 4. nginx
            nginx_conf = Path(ADMIN_BACKUP_NGINX_CONF)
            if nginx_conf.is_file():
                archive.add(nginx_conf, arcname="config/nginx-ocinka.pro.conf")

        return FileResponse(
            path=str(tmp_path),
            filename=f"ocinka-backup-{timestamp}.tar.gz",
            media_type="application/gzip",
            background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
        )
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


@admin_router.get("/dashboard")
async def admin_dashboard(admin: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    """Адмін: загальна статистика"""
    total_users = (await db.execute(select(func.count(User.id)))).scalar()
    total_reports = (await db.execute(select(func.count(Report.id)))).scalar()
    ready_reports = (await db.execute(
        select(func.count(Report.id)).where(Report.status == ReportStatus.READY)
    )).scalar()
    
    return {
        "total_users": total_users,
        "total_reports": total_reports,
        "ready_reports": ready_reports,
    }


# Plan ceilings, mirroring the check in the report-creation endpoint.
# Kept as a module constant so the admin panel and the enforcement path cannot
# drift apart silently: if one is edited and the other is not, the panel would
# show a limit the server does not apply.
ADMIN_PLAN_LIMITS = {"free": 3, "basic": 10, "standard": 30, "pro": 60, "team": 9999}


@admin_router.get("/users")
async def admin_users(admin: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    """Admin list with subscription, concise contact data and report summary."""
    result = await db.execute(select(User).order_by(User.created_at.desc()).limit(100))
    users = result.scalars().all()
    rows = []
    for u in users:
        reports = list(u.reports or [])
        ready = [r for r in reports if r.status == ReportStatus.READY]
        by_object = {"apartment": 0, "house": 0, "land": 0}
        by_deal = {"eoselia": 0, "mortgage": 0, "cash": 0, "other": 0}
        city = None
        for report in sorted(reports, key=lambda item: item.created_at or datetime.min, reverse=True):
            obj = report.object_type.value if hasattr(report.object_type, "value") else str(report.object_type)
            if obj in by_object:
                by_object[obj] += 1
            deal = report.deal_type.value if hasattr(report.deal_type, "value") else str(report.deal_type)
            by_deal[deal if deal in by_deal else "other"] += 1
            if not city:
                city = (report.ocr_raw or {}).get("city")
        plan = u.plan.value if hasattr(u.plan, "value") else str(u.plan or "free")
        active_subscription = bool(u.is_active) and (
            u.subscription_active_until is None or u.subscription_active_until >= datetime.utcnow()
        )
        stuck_age = timedelta(hours=ADMIN_STUCK_REPORT_HOURS)
        reports_stuck = sum(
            1 for r in reports
            if r.status not in (ReportStatus.READY,)
            and r.created_at and (datetime.utcnow() - r.created_at) > stuck_age
        )
        rows.append({
            "id": str(u.id), "email": u.email, "name": u.full_name, "phone": u.phone,
            "city": city, "is_active": bool(u.is_active), "subscription_active": active_subscription,
            "plan": plan, "reports_this_month": u.reports_this_month or 0,
            # Quota, as the user themselves sees it.
            #
            # Which counter applies depends on the plan: the free plan spends
            # free_reports_left, paid plans count reports_this_month against
            # the plan ceiling. Only the paid one was exposed here, so for a
            # free user the panel showed no quota at all and there was no way
            # to tell whether someone had generations remaining.
            "free_reports_left": max(0, u.free_reports_left or 0),
            "plan_limit": ADMIN_PLAN_LIMITS.get(plan, 3),
            "reports_left": (
                max(0, u.free_reports_left or 0)
                if plan == SubscriptionPlan.FREE.value
                else max(0, ADMIN_PLAN_LIMITS.get(plan, 3) - (u.reports_this_month or 0))
            ),
            "quota_exhausted": (
                (u.free_reports_left or 0) <= 0
                if plan == SubscriptionPlan.FREE.value
                else (u.reports_this_month or 0) >= ADMIN_PLAN_LIMITS.get(plan, 3)
            ),
            "reports_total": len(reports), "reports_ready": len(ready),
            "reports_stuck": reports_stuck,
            "objects": by_object, "deals": by_deal,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "subscription_active_until": u.subscription_active_until.isoformat() if u.subscription_active_until else None,
        })
    return rows


@admin_router.get("/users/{user_id}")
async def admin_user_detail(user_id: str, admin: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    """Admin drill-down: registration/login times, per-report stage and stall
    reason, and a recent activity feed — everything needed to see why a
    specific user's report isn't finished without touching server logs."""
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Користувача не знайдено")

    user = (await db.execute(select(User).where(User.id == user_uuid))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Користувача не знайдено")

    reports = list(user.reports or [])
    reports_sorted = sorted(reports, key=lambda item: item.created_at or datetime.min, reverse=True)

    activity_result = await db.execute(
        select(ActivityLog)
        .where(ActivityLog.user_id == user_uuid)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
    )
    activity = activity_result.scalars().all()

    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.full_name,
        "phone": user.phone,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "reports": [_admin_report_detail(r) for r in reports_sorted],
        "activity": [
            {
                "action": a.action,
                "action_label": ADMIN_ACTIVITY_LABELS.get(a.action, a.action),
                "details": a.details,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in activity
        ],
    }


@admin_router.get("/agents")
async def admin_agents(admin: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    """Agency/agent registrations and manually maintained 10% partner fields."""
    result = await db.execute(select(AgencyReferral).order_by(AgencyReferral.created_at.desc()).limit(200))
    agents = result.scalars().all()
    return [{
        "id": str(agent.id), "user_id": str(agent.user_id), "agency_name": agent.agency_name,
        "contact_person": agent.contact_person, "email": agent.contact_email,
        "phone": agent.contact_phone, "status": agent.status,
        "referral_code": agent.referral_code, "team_size": agent.team_size,
        "reports_per_month": agent.reports_per_month,
        "partner_percent": agent.partner_percent if agent.partner_percent is not None else 10,
        "partner_accrued_uah": agent.partner_accrued_uah or 0,
        "partner_paid_uah": agent.partner_paid_uah or 0,
        "is_partner_active": bool(agent.is_partner_active),
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    } for agent in agents]


@admin_router.patch("/agents/{agent_id}")
async def update_agent_accounting(
    agent_id: str, req: PartnerAccountingRequest,
    admin: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AgencyReferral).where(AgencyReferral.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, "Агента не знайдено")
    for field in ("status", "is_partner_active", "partner_percent", "partner_accrued_uah", "partner_paid_uah"):
        value = getattr(req, field)
        if value is not None:
            setattr(agent, field, value)
    db.add(ActivityLog(user_id=admin.id, action="update_partner_accounting", details={"agent_id": agent_id}))
    return {"ok": True}
