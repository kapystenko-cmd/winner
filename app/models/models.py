"""Моделі бази даних — користувачі, звіти, аналоги, реферали"""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, 
    ForeignKey, Enum as SQLEnum, JSON
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.core.database import Base
import enum


# ========== ENUM ==========

class ReportStatus(str, enum.Enum):
    UPLOADING = "uploading"           # Файли завантажуються
    OCR_PROCESSING = "ocr_processing" # Розпізнавання документів
    OCR_FAILED = "ocr_failed"         # OCR не зміг — потрібен ручний ввід
    ANALOGS_SEARCH = "analogs_search" # Пошук аналогів
    CALCULATING = "calculating"       # Розрахунок вартості
    GENERATING = "generating"         # Генерація документів
    READY = "ready"                   # Готово — можна завантажити
    ERROR = "error"                   # Помилка

class EvalMode(str, enum.Enum):
    STANDARD = "standard"
    CONSERVATIVE = "conservative"
    OPTIMISTIC = "optimistic"

class DealType(str, enum.Enum):
    CASH = "cash"
    MORTGAGE = "mortgage"
    EOSELIA = "eoselia"
    GIFT = "gift"
    INHERITANCE = "inheritance"
    CERTIFICATE = "certificate"

class ObjectType(str, enum.Enum):
    APARTMENT = "apartment"
    HOUSE = "house"
    LAND = "land"
    COMMERCIAL = "commercial"

class ValueSelectionMode(str, enum.Enum):
    AUTOMATIC = "automatic"
    PROFESSIONAL = "professional"
    EXPERT = "expert"

class SubscriptionPlan(str, enum.Enum):
    FREE = "free"
    BASIC = "basic"
    STANDARD = "standard"
    PRO = "pro"
    TEAM = "team"


# ========== МОДЕЛІ ==========

class User(Base):
    __tablename__ = "users"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    phone = Column(String(20))
    password_hash = Column(String(255), nullable=False)
    
    # Оцінювач
    full_name = Column(String(255), nullable=False)
    cert_number = Column(String(100))          # Номер свідоцтва
    cert_date = Column(DateTime)               # Дата видачі
    specializations = Column(JSON, default=[]) # ["realty", "land", ...]
    
    # СОД
    sod_name = Column(String(500))             # Повна назва
    sod_edrpou = Column(String(20))            # ЄДРПОУ
    sod_cert_number = Column(String(100))      # Сертифікат ФДМУ
    sod_cert_date = Column(DateTime)
    sod_address = Column(String(500))          # Юр. адреса
    sod_header_offset_mm = Column(Integer, default=0) # Відступ під бланк
    profile_document_files = Column(JSON, default=list)
    
    # Підписка
    plan = Column(SQLEnum(SubscriptionPlan), default=SubscriptionPlan.FREE)
    reports_this_month = Column(Integer, default=0)
    free_reports_left = Column(Integer, default=3)
    subscription_active_until = Column(DateTime)
    
    # Маркетинг
    newsletter_consent = Column(Boolean, default=False)
    
    # Мета
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime)
    
    # Зв'язки
    reports = relationship("Report", back_populates="user", lazy="selectin")
    referral = relationship("AgencyReferral", back_populates="user", uselist=False)


class Report(Base):
    __tablename__ = "reports"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    # Тип
    object_type = Column(SQLEnum(ObjectType), default=ObjectType.APARTMENT)
    deal_type = Column(SQLEnum(DealType), default=DealType.CASH)
    eval_mode = Column(SQLEnum(EvalMode), default=EvalMode.STANDARD)
    
    # Дані об'єкта (з OCR)
    address = Column(String(500))
    area_sqm = Column(Float)
    rooms = Column(Integer)
    floor = Column(Integer)
    total_floors = Column(Integer)
    cadastral_number = Column(String(50))
    year_built = Column(Integer)
    
    # Результати
    status = Column(SQLEnum(ReportStatus), default=ReportStatus.UPLOADING)
    estimated_value = Column(Float)          # Оціночна вартість
    range_min = Column(Float)                # Нижня межа діапазону
    range_max = Column(Float)                # Верхня межа
    benchmark_fdmu = Column(Float)           # Бенчмарк ФДМУ
    adjustment_percent = Column(Float)       # % коригування від бенчмарку
    recommended_value = Column(Float)
    selected_value = Column(Float)
    # The PostgreSQL migration stores the public values (automatic,
    # professional, expert), not Python enum member names.  Explicit mapping
    # is required when existing reports are loaded during login.
    value_selection_mode = Column(
        SQLEnum(
            ValueSelectionMode,
            name="value_selection_mode",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=ValueSelectionMode.AUTOMATIC,
    )
    value_deviation_reason = Column(Text)
    valuation_statistics = Column(JSON)
    search_journal = Column(JSON)
    report_options = Column(JSON, default={"include_screenshots": False, "format": "table"})
    object_photo_files = Column(JSON, default=[])
    # Optional evidence supplied by the appraiser.  The service never fetches
    # an e-certificate from the state system on the appraiser's behalf.
    e_certificate_files = Column(JSON, default=[])
    location_map_files = Column(JSON, default=[])
    object_listing_url = Column(String(1000))
    
    # Файли
    upload_files = Column(JSON, default=[])   # Шляхи завантажених сканів
    word_path = Column(String(500))           # Шлях до Word
    pdf_conclusion_path = Column(String(500)) # PDF висновок
    pdf_full_path = Column(String(500))       # PDF повний пакет
    
    # OCR дані (сирі)
    ocr_raw = Column(JSON)                    # Повний результат OCR
    ocr_provider = Column(String(50))         # gemini / claude / manual
    
    # Мета
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    expires_at = Column(DateTime)             # Коли видаляти (6 міс)
    
    # Зв'язки
    user = relationship("User", back_populates="reports")
    analogs = relationship("Analog", back_populates="report", lazy="selectin", cascade="all, delete-orphan")


class Analog(Base):
    __tablename__ = "analogs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("reports.id"), nullable=False)
    
    source = Column(String(50))               # dimria / olx / manual
    url = Column(String(1000))
    title = Column(String(500))
    price_uah = Column(Float)
    price_per_sqm = Column(Float)
    area_sqm = Column(Float)
    rooms = Column(Integer)
    floor = Column(Integer)
    address = Column(String(500))
    
    screenshot_path = Column(String(500))     # Шлях до скриншоту
    screenshot_verified = Column(Boolean, default=False) # Gemini перевірив
    
    is_selected = Column(Boolean, default=True) # Обрано у звіт
    rank = Column(Integer)                     # Позиція (1-5)
    
    raw_data = Column(JSON)                    # Повні дані з API
    created_at = Column(DateTime, default=datetime.utcnow)
    
    report = relationship("Report", back_populates="analogs")


class AgencyReferral(Base):
    """Реферальна програма для агентств нерухомості"""
    __tablename__ = "agency_referrals"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # An agency may leave a partnership request before creating an evaluator account.
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    
    agency_name = Column(String(500))
    contact_person = Column(String(255))
    contact_email = Column(String(255))
    contact_phone = Column(String(20))
    reports_per_month = Column(Integer)
    team_size = Column(Integer)
    message = Column(Text)
    
    status = Column(String(50), default="pending")  # pending / approved / rejected
    referral_code = Column(String(50), unique=True)
    discount_percent = Column(Integer, default=0)

    # Partner accounting is entered and controlled by an administrator.
    # It intentionally does not initiate any payment automatically.
    partner_percent = Column(Float, default=10.0)
    partner_accrued_uah = Column(Float, default=0.0)
    partner_paid_uah = Column(Float, default=0.0)
    is_partner_active = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="referral")


class ActivityLog(Base):
    """Журнал дій — для доказової бази"""
    __tablename__ = "activity_log"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    
    action = Column(String(100), nullable=False)  # register, login, upload, generate, download
    details = Column(JSON)
    ip_address = Column(String(50))
    user_agent = Column(String(500))
    
    created_at = Column(DateTime, default=datetime.utcnow)


class ValueChangeLog(Base):
    __tablename__ = "value_change_log"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("reports.id"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    previous_value = Column(Float)
    selected_value = Column(Float, nullable=False)
    # Keep the audit log compatible with PostgreSQL values created by the
    # migration: automatic / professional / expert, rather than enum names.
    selection_mode = Column(
        SQLEnum(
            ValueSelectionMode,
            name="value_selection_mode",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
    )
    reason = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
