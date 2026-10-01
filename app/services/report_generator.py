"""Генерацiя звiтiв: Word + PDF Висновок + PDF повний пакет"""
import os
import re
import shutil
import subprocess
import tempfile
from io import BytesIO
from datetime import datetime
from pathlib import Path
from docx import Document
from docx.shared import Cm, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import black, grey, lightgrey, HexColor
from reportlab.lib.utils import ImageReader
from PIL import Image as PILImage
from pypdf import PdfWriter, PdfReader
from app.core.config import settings


def _report_date(report) -> str:
    """Use the evaluator-selected valuation date, otherwise today's date."""
    selected = ((getattr(report, "report_options", None) or {}).get("valuation_date") or "").strip()
    if selected:
        try:
            return datetime.strptime(selected, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            pass
    return datetime.now().strftime("%d.%m.%Y")


def _reg_fonts():
    """Use Times New Roman where available, with a Cyrillic serif fallback."""
    fonts = {"n": "Helvetica", "b": "Helvetica-Bold"}
    normal_candidates = [
        ("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf", "TimesNewRoman"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf", "LiberationSerif"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", "DejaVuSerif"),
        ("C:/Windows/Fonts/times.ttf", "TimesNewRoman"),
    ]
    bold_candidates = [
        ("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Bold.ttf", "TimesNewRomanBold"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf", "LiberationSerifBold"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", "DejaVuSerifBold"),
        ("C:/Windows/Fonts/timesbd.ttf", "TimesNewRomanBold"),
    ]
    for path, name in normal_candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                fonts["n"] = name
                break
            except Exception:
                pass
    for path, name in bold_candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                fonts["b"] = name
                break
            except Exception:
                pass
    return fonts


async def _get_usd_rate():
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get("https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json")
            if resp.status_code == 200:
                d = resp.json()
                if d:
                    return float(d[0]["rate"])
    except:
        pass
    return 41.5


# ========== WORD ==========

def _add_hyperlink(paragraph, url, label="Відкрити"):
    """Create an actual external hyperlink instead of relying on Word auto-detection."""
    if not url:
        paragraph.add_run("-")
        return
    relation_id = paragraph.part.relate_to(
        url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relation_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    props.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(underline)
    run.append(props)
    text = OxmlElement("w:t")
    text.text = label
    run.append(text)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


_FIELD_LABELS = {
    "address": "адреса", "street": "вулиця", "building": "номер будинку",
    "apartment": "номер квартири", "area_total": "загальна площа",
    "area_living": "житлова площа", "area_kitchen": "площа кухні",
    "owner_name": "власник", "rooms": "кількість кімнат", "floor": "поверх",
    "total_floors": "поверховість", "registration_number": "реєстраційний номер",
}

_DOCUMENT_TYPE_LABELS = {
    "technical passport": "Технічний паспорт",
    "tech passport": "Технічний паспорт",
    "technical inventory": "Технічний паспорт",
    "ownership document": "Правовстановлюючий документ",
    "title document": "Правовстановлюючий документ",
    "ownership certificate": "Свідоцтво про право власності",
    "certificate of ownership": "Свідоцтво про право власності",
    "state register extract": "Витяг з Державного реєстру речових прав",
    "registry extract": "Витяг з Державного реєстру речових прав",
    "passport": "Паспорт власника",
    "tax identification card": "РНОКПП власника",
    "id card": "ID-картка власника",
    "cadastral extract": "Витяг з Державного земельного кадастру",
    "consolidated valuation act": "Зведений акт вартості будівель, господарських будівель та споруд",
    "buildings valuation act": "Зведений акт вартості будівель, господарських будівель та споруд",
}


def _document_label(value, fallback):
    """Use Ukrainian annex captions even when OCR returned an English type."""
    raw = str(value or "").strip()
    normalized = raw.casefold()
    for source, ukrainian in _DOCUMENT_TYPE_LABELS.items():
        if source in normalized:
            return ukrainian
    return raw or fallback


def _review_fields(report):
    return {str(item.get("field", "")).replace("address (вулиця)", "address")
            for item in ((report.ocr_raw or {}).get("_conflicts") or [])}


def _add_review_notice_word(doc, report):
    fields = _review_fields(report)
    if not fields:
        return
    p = doc.add_paragraph()
    run = p.add_run("УВАГА: ЧЕРНЕТКА ПОТРЕБУЄ ПЕРЕВІРКИ ОЦІНЮВАЧЕМ. ")
    run.bold = True; run.underline = True
    listed = ", ".join(_FIELD_LABELS.get(field, field) for field in fields)
    mark = p.add_run("Виявлені розбіжності: " + listed + ". Позначені дані мають бути перевірені за першоджерелами перед підписанням або реєстрацією.")
    mark.bold = True; mark.underline = True


def _pdf_review_notice(c, report, y, width):
    fields = _review_fields(report)
    if not fields:
        return y
    c.setFont(_reg_fonts()["b"], 8)
    c.drawString(2.15 * cm, y - 0.45 * cm, "УВАГА: чернетка містить дані для перевірки оцінювачем перед підписанням.")
    labels = ", ".join(_FIELD_LABELS.get(field, field) for field in fields)
    c.drawString(2.15 * cm, y - 0.78 * cm, "Розбіжності: " + labels[:115])
    c.line(2.15 * cm, y - 0.84 * cm, width - 2 * cm, y - 0.84 * cm)
    return y - 1.35 * cm


def _ua_plural(number, forms):
    """Select the Ukrainian singular/few/many form."""
    number = abs(int(number)) % 100
    if 11 <= number <= 14:
        return forms[2]
    number %= 10
    if number == 1:
        return forms[0]
    if 2 <= number <= 4:
        return forms[1]
    return forms[2]


def _ua_triplet(number: int, feminine: bool = False) -> list[str]:
    units = ["", "один", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
    if feminine:
        units[1], units[2] = "одна", "дві"
    teens = ["десять", "одинадцять", "дванадцять", "тринадцять", "чотирнадцять", "п'ятнадцять", "шістнадцять", "сімнадцять", "вісімнадцять", "дев'ятнадцять"]
    tens = ["", "", "двадцять", "тридцять", "сорок", "п'ятдесят", "шістдесят", "сімдесят", "вісімдесят", "дев'яносто"]
    hundreds = ["", "сто", "двісті", "триста", "чотириста", "п'ятсот", "шістсот", "сімсот", "вісімсот", "дев'ятьсот"]
    words = []
    if number // 100:
        words.append(hundreds[number // 100])
    rest = number % 100
    if 10 <= rest <= 19:
        words.append(teens[rest - 10])
    else:
        if rest // 10:
            words.append(tens[rest // 10])
        if rest % 10:
            words.append(units[rest % 10])
    return words


def _amount_in_words(value) -> str:
    """Format a rounded UAH value in Ukrainian words for the conclusion."""
    try:
        amount = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return "Сума не визначена"
    if amount == 0:
        return "Нуль гривень 00 копійок"
    groups = [
        (1_000_000_000, False, ("мільярд", "мільярди", "мільярдів")),
        (1_000_000, False, ("мільйон", "мільйони", "мільйонів")),
        (1_000, True, ("тисяча", "тисячі", "тисяч")),
        (1, True, ("гривня", "гривні", "гривень")),
    ]
    words = []
    currency_added = False
    remainder = amount
    for divisor, feminine, forms in groups:
        part, remainder = divmod(remainder, divisor)
        if not part:
            continue
        words.extend(_ua_triplet(part, feminine=feminine))
        words.append(_ua_plural(part, forms))
        if divisor == 1:
            currency_added = True
    if not currency_added:
        words.append("гривень")
    return " ".join(words).capitalize() + " 00 копійок"


def _draw_conclusion_table(c, rows, y, page_width, font_name, bold_font):
    """Draw the bordered fact table used in a notarial valuation conclusion."""
    x = 1.3 * cm
    total_width = page_width - 2.6 * cm
    # Prevent long labels in the one-page conclusion from touching values.
    label_width = 6.35 * cm
    value_x = x + label_width
    c.setStrokeColor(grey)
    for label, value in rows:
        value_lines = _wrap(str(value or "—"), 62)
        height = max(0.8 * cm, (len(value_lines) * 0.39 + 0.28) * cm)
        c.rect(x, y - height, label_width, height, stroke=1, fill=0)
        c.rect(value_x, y - height, total_width - label_width, height, stroke=1, fill=0)
        c.setFont(bold_font, 8.7)
        for index, line in enumerate(_wrap(label, 30)):
            c.drawString(x + 0.18 * cm, y - 0.34 * cm - index * 0.34 * cm, line)
        c.setFont(font_name, 8.7)
        for index, line in enumerate(value_lines):
            c.drawString(value_x + 0.18 * cm, y - 0.34 * cm - index * 0.34 * cm, line)
        y -= height
    return y


def _draw_clickable_url(c, url, x, y, font_name, font_size=8.4, max_chars=94):
    """Render a complete URL and attach a clickable PDF annotation to it."""
    value = str(url or "")
    if not value:
        return y
    c.setFont(font_name, font_size)
    c.setFillColor(HexColor("#0563C1"))
    for line in _wrap(value, max_chars):
        c.drawString(x, y, line)
        width = pdfmetrics.stringWidth(line, font_name, font_size)
        c.linkURL(value, (x, y - 2, x + width, y + font_size + 2), relative=0)
        c.line(x, y - 1.2, x + width, y - 1.2)
        y -= max(0.33 * cm, (font_size / 72) * cm * 1.3)
    c.setFillColor(black)
    return y


def _document_entries(report, user):
    """Collect saved scans without modifying the report's audit trail."""
    entries = []
    seen = set()

    def add(path, label, page_orientations=None):
        path = str(path or "")
        if path and path not in seen and os.path.isfile(path):
            seen.add(path)
            entries.append((path, label, page_orientations))

    extracts = (report.ocr_raw or {}).get("_document_extracts", [])
    # Upload names can arrive with different casing or a temporary directory
    # prefix. Match by a normalised basename so OCR orientation metadata is
    # attached to the same scan that is inserted into the package.
    extracts_by_filename = {
        Path(str(item.get("_file_name"))).name.casefold(): item
        for item in extracts
        if isinstance(item, dict) and item.get("_file_name")
    }
    for index, path in enumerate(report.upload_files or [], start=1):
        # A failed OCR call must not shift orientation information onto the
        # next upload. Never use a positional fallback: a page-orientation
        # hint from another file can rotate a correct scan upside down.
        extract = extracts_by_filename.get(Path(str(path)).name.casefold())
        if not isinstance(extract, dict):
            extract = {}
        # The document-type guess comes from OCR and is occasionally wrong on
        # a specific scan (a "Свідоцтво" photo classified as "Технічний
        # паспорт" was seen on a real report) — that is a model
        # misclassification, not a positional/indexing bug, and cannot be
        # fully eliminated. Prefixing the upload's own sequence number makes
        # a wrong guess immediately checkable against what was actually
        # uploaded Nth, instead of silently reading as a different document.
        guessed = _document_label(extract.get("document_type"), "")
        label = f"Документ {index}: {guessed}" if guessed else f"Документ {index}"
        add(path, label, extract.get("page_orientations"))
    # Object photographs are handled separately as contact sheets in every
    # package.  Never add them here: otherwise the basic/evidence packages
    # would append one photograph per page after the report, while the expert
    # package used a proper 2-by-3 sheet.
    for index, path in enumerate(getattr(report, "location_map_files", None) or [], start=1):
        add(path, f"Карта розташування об'єкта {index}")
    for index, path in enumerate(getattr(report, "e_certificate_files", None) or [], start=1):
        add(path, f"Е-довідка ФДМУ про оціночну вартість {index}")
    if (report.report_options or {}).get("include_appraiser_documents", True):
        labels = {
            "appraiser_certificate": "Кваліфікаційне свідоцтво оцінювача",
            "appraiser_certificate_real_estate": "Кваліфікаційне свідоцтво — нерухомість (ФДМУ)",
            "appraiser_certificate_land": "Кваліфікаційне свідоцтво — земельні ділянки (Держкомзем)",
            "continuing_education": "Документ про підвищення кваліфікації оцінювача",
            "sod_certificate": "Сертифікат СОД / ФДМУ",
            "sod_statutory": "Установчий документ СОД",
            "other": "Інший документ оцінювача / СОД",
        }
        for index, item in enumerate(user.profile_document_files or [], start=1):
            if isinstance(item, dict) and item.get("kind") != "sod_logo":
                kind = str(item.get("kind") or "other")
                add(item.get("path"), labels.get(kind, "Документ оцінювача") + f" {index}")
    # Profile documents are OCRed once when uploaded.  Their stored
    # orientation metadata must travel with the annex entry just as it does
    # for the object-document OCR extracts above.
    profile_orientations = {
        str(Path(str(item.get("path"))).resolve()): item.get("page_orientations")
        for item in (user.profile_document_files or [])
        if isinstance(item, dict) and item.get("path")
    }
    entries = [
        (path, label, orientations or profile_orientations.get(str(Path(path).resolve())))
        for path, label, orientations in entries
    ]
    return entries


def _profile_logo_path(user):
    """Latest image logo is used as a small title mark, never as evidence."""
    for item in reversed(getattr(user, "profile_document_files", None) or []):
        if not isinstance(item, dict) or item.get("kind") != "sod_logo":
            continue
        path = str(item.get("path") or "")
        if path.lower().endswith((".jpg", ".jpeg", ".png")) and os.path.isfile(path):
            return path
    return None


def _rotation_hint_for_page(page_orientations, page_number: int) -> int | None:
    """Return a high-confidence clockwise correction based on readable text.

    OCR records this only as presentation metadata. A low-confidence answer
    is deliberately ignored: a wrong automatic rotation is worse than leaving
    the uploaded scan untouched.
    """
    if isinstance(page_orientations, dict):
        page_orientations = [page_orientations]
    if not isinstance(page_orientations, list):
        return None
    for item in page_orientations:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("page", 1)) != page_number:
                continue
            confidence = float(item.get("confidence", 0))
            degrees = int(item.get("rotate_clockwise_degrees", 0)) % 360
        except (TypeError, ValueError):
            continue
        # OCR is asked explicitly for the reading direction of each source
        # page. 0.50 is the threshold — if Gemini can determine orientation
        # at all, it should be applied. Previously 0.70 was too conservative
        # and left clearly rotated scans untouched because Gemini returned
        # confidence 0.60-0.69 on complex scanned documents.
        if confidence >= 0.50 and degrees in (0, 90, 180, 270):
            return degrees
    return None


def _rotation_for_page(page_orientations, page_number: int) -> int:
    """Return an available text-orientation correction, otherwise zero."""
    return _rotation_hint_for_page(page_orientations, page_number) or 0


def _local_text_rotation(source) -> int:
    """Last-resort orientation check for image scans, when installed locally.

    Tesseract OSD reads the direction of text without calling another paid
    service.  It is used only where the primary OCR did not provide an
    orientation hint and only with a minimally credible OSD confidence.
    """
    executable = shutil.which("tesseract")
    if not executable:
        return 0
    try:
        result = subprocess.run(
            [executable, str(source), "stdout", "--psm", "0"],
            capture_output=True,
            text=True,
            timeout=18,
            check=False,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        angle = re.search(r"Rotate:\s*(\d+)", output, flags=re.IGNORECASE)
        confidence = re.search(r"Orientation confidence:\s*([\d.]+)", output, flags=re.IGNORECASE)
        if angle and confidence and float(confidence.group(1)) >= 1.2:
            degrees = int(angle.group(1)) % 360
            if degrees in (90, 180, 270):
                return degrees

        # OSD is conservative and frequently refuses a phone photo with a
        # small amount of text.  In that case compare OCR readability for all
        # four directions.  This is a local, display-only operation and never
        # changes the uploaded evidence file.
        from PIL import Image, ImageOps
        scores: dict[int, int] = {}
        with Image.open(source) as original, tempfile.TemporaryDirectory(prefix="ocinka-orientation-") as work:
            image = ImageOps.exif_transpose(original).convert("RGB")
            for clockwise in (0, 90, 180, 270):
                candidate = Path(work) / f"orientation-{clockwise}.png"
                image.rotate(-clockwise, expand=True).save(candidate, format="PNG")
                probe = subprocess.run(
                    [executable, str(candidate), "stdout", "-l", "ukr+rus+eng", "--psm", "6"],
                    capture_output=True, text=True, timeout=12, check=False,
                )
                recognised = probe.stdout or ""
                letters = len(re.findall(r"[А-Яа-яІіЇїЄєA-Za-z]", recognised))
                words = len(re.findall(r"[А-Яа-яІіЇїЄєA-Za-z]{3,}", recognised))
                scores[clockwise] = letters + words * 5
        best = max(scores, key=scores.get, default=0)
        baseline = scores.get(0, 0)
        # Only rotate when readability is materially better than the upload's
        # original orientation; noisy scans otherwise stay unchanged.
        return best if best and scores[best] >= max(18, baseline + 12) else 0
    except Exception as error:
        print("Local scan orientation check unavailable: " + str(error))
        return 0


def _verified_text_rotation(source) -> int | None:
    """Return the clockwise correction that makes scan text upright.

    This is deliberately performed on an EXIF-normalised temporary image.
    Testing the original camera bytes and then rotating an EXIF-normalised
    image was the reason some annexes could remain sideways or upside down.
    The uploaded file is never changed.
    """
    executable = shutil.which("tesseract")
    if not executable:
        return None
    try:
        from PIL import Image, ImageOps

        scores: dict[int, int] = {}
        with Image.open(source) as original, tempfile.TemporaryDirectory(prefix="ocinka-orientation-v2-") as work:
            image = ImageOps.exif_transpose(original).convert("RGB")
            # Orientation checks do not need the original 10–25 MP camera
            # image.  A compact copy is faster and prevents each attachment
            # from blocking full-package creation for nearly a minute.
            image.thumbnail((1100, 1100))
            normalised = Path(work) / "normalised.jpg"
            image.save(normalised, format="JPEG", quality=82, optimize=True)

            # OSD is the strongest signal when a scan contains enough text.
            # Tesseract's "Rotate" value is the clockwise correction needed
            # to make its detected text baseline horizontal.
            osd = subprocess.run(
                [executable, str(normalised), "stdout", "--psm", "0"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            osd_text = (osd.stdout or "") + "\n" + (osd.stderr or "")
            angle = re.search(r"Rotate:\s*(\d+)", osd_text, flags=re.IGNORECASE)
            confidence = re.search(r"Orientation confidence:\s*([\d.]+)", osd_text, flags=re.IGNORECASE)
            # OSD's low values are guesses on scans with stamps or photos.
            if angle and confidence and float(confidence.group(1)) >= 5.0:
                correction = int(angle.group(1)) % 360
                if correction in (0, 90, 180, 270):
                    return correction

            # OSD may abstain on phone scans. Compare recognised words and
            # their confidence in all four directions instead of merely
            # counting arbitrary characters.
            for clockwise in (0, 90, 180, 270):
                candidate = Path(work) / f"orientation-{clockwise}.jpg"
                image.rotate(-clockwise, expand=True).save(candidate, format="JPEG", quality=80, optimize=True)
                probe = subprocess.run(
                    [executable, str(candidate), "stdout", "-l", "ukr+rus+eng", "--psm", "6", "tsv"],
                    capture_output=True, text=True, timeout=4, check=False,
                )
                confidence_total = 0.0
                word_count = 0
                for row in (probe.stdout or "").splitlines()[1:]:
                    columns = row.split("\t")
                    if len(columns) < 12:
                        continue
                    try:
                        word_confidence = float(columns[10])
                    except (TypeError, ValueError):
                        continue
                    word = columns[11].strip()
                    if word_confidence >= 15 and len(re.findall(r"[A-Za-z\u0400-\u052F]", word)) >= 2:
                        confidence_total += word_confidence
                        word_count += 1
                scores[clockwise] = confidence_total * 1.6 + word_count * 18.0
        best = max(scores, key=scores.get, default=0)
        baseline = scores.get(0, 0)
        # A meaningful score at 0 explicitly means that text is already
        # upright. ``None`` means "not enough evidence", so a Gemini hint
        # cannot override a locally confirmed upright scan.
        if best == 0 and baseline >= 90:
            return 0
        if best and scores[best] >= max(115, baseline * 1.35 + 30):
            return best
        return None
    except Exception as error:
        print("Verified scan orientation check unavailable: " + str(error))
        return None


def _render_pdf_page_for_annex(source, page_number: int, temporary) -> Path | None:
    """Render the source page before orienting it for a stable final PDF.

    Changing the /Rotate flag of uploaded PDF pages is not reliable: phone
    scanner apps often already store such a flag, and viewers combine flags
    differently. A rendered page has one unambiguous visual orientation.
    """
    converter = shutil.which("pdftoppm")
    if not converter:
        return None
    try:
        output = Path(source).with_name(f"_annex_render_{len(temporary) + 1}_{page_number}")
        completed = subprocess.run(
            [converter, "-f", str(page_number), "-l", str(page_number), "-r", "150", "-jpeg", "-jpegopt", "quality=85", "-singlefile", str(source), str(output)],
            capture_output=True, text=True, timeout=45, check=False,
        )
        image = output.with_suffix(".jpg")
        if completed.returncode == 0 and image.is_file():
            temporary.append(image)
            return image
        print("PDF page rendering failed: " + (completed.stderr or "unknown error")[:240])
        return None
    except Exception as error:
        print("PDF page rendering unavailable: " + str(error))
        return None


def _ask_gemini_orientation(image_path) -> int | None:
    """Dedicated Gemini Vision call for page orientation.

    Sends the scan in FOUR rotations (0, 90, 180, 270) and asks Gemini
    which one has normally readable text. Much more reliable than asking
    for an absolute angle or comparing only two variants.
    """
    try:
        from app.core.config import settings
        if not settings.gemini_api_key:
            return None
        import google.generativeai as genai
        from PIL import Image as _PILImg
        import io as _io

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(settings.gemini_model)

        with _PILImg.open(image_path) as orig:
            orig = orig.convert("RGB")

            variants = {}
            for label, angle in [("A", 0), ("B", 90), ("C", 180), ("D", 270)]:
                rotated = orig.rotate(-angle, expand=True) if angle else orig
                buf = _io.BytesIO()
                rotated.save(buf, format="JPEG", quality=80)
                variants[label] = buf.getvalue()

        prompt = (
            "Тобі показано ЧОТИРИ варіанти одного й того ж сканованого документа, "
            "кожен повернутий на різний кут.\n\n"
            "Визнач, в якому варіанті ОСНОВНИЙ ТЕКСТ документа читається "
            "ПРАВИЛЬНО — тобто людина може читати його зверху вниз, зліва направо, "
            "без потреби повертати голову.\n\n"
            "Дивись на ДРУКОВАНИЙ ТЕКСТ (заголовки, рядки тексту, дати, адреси), "
            "а НЕ на печатки, штампи, голограми чи водяні знаки.\n\n"
            "Відповідай ТІЛЬКИ одною літерою: A, B, C або D."
        )

        parts = [prompt]
        for label in ["A", "B", "C", "D"]:
            parts.append(f"=== ВАРІАНТ {label}: ===")
            parts.append({"mime_type": "image/jpeg", "data": variants[label]})

        def _call():
            return model.generate_content(parts, request_options={"timeout": 30})

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(_call)
            response = future.result(timeout=35)

        text = (response.text or "").strip().upper()
        angle_map = {"A": 0, "B": 90, "C": 180, "D": 270}
        for letter, angle in angle_map.items():
            if letter in text:
                other_letters = [l for l in angle_map if l != letter and l in text]
                if not other_letters:
                    print(f"Dedicated Gemini orientation: variant {letter} ({angle}deg) for {Path(image_path).name}")
                    return angle
        print(f"Dedicated Gemini orientation: ambiguous \'{text[:30]}\' for {Path(image_path).name}")
        return None
    except Exception as e:
        print(f"Dedicated Gemini orientation failed: {e}")
        return None


def _prepare_image_for_annex(
    source,
    page_orientations,
    temporary,
    page_number: int = 1,
    enable_text_orientation: bool = True,
    max_size: tuple = (1280, 1800),
    jpeg_quality: int = 78,
):
    """Make a display-only upright copy of a scan without changing upload."""
    try:
        from PIL import Image, ImageOps

        with Image.open(source) as original:
            image = ImageOps.exif_transpose(original)
            gemini_correction = _rotation_hint_for_page(page_orientations, page_number)
            local_correction = (
                _verified_text_rotation(source)
                if enable_text_orientation and gemini_correction is None else None
            )
            correction = gemini_correction if gemini_correction is not None else local_correction
            # Analog screenshots are web-page captures: they are always
            # portrait and in the correct orientation already. Running the
            # 4-variant Gemini check on them is actively harmful — it has
            # rotated screenshots 90° thinking it was "unlocking" readable
            # text, which then put the price and specs off the frame after
            # resize. For callers that passed enable_text_orientation=False
            # the whole orientation pipeline is skipped.
            if not enable_text_orientation:
                correction = 0
                dedicated = None
            else:
                # ALWAYS run the dedicated 4-variant Gemini check for document
                # scans. The OCR orientation hint (gemini_correction) proved
                # unreliable on real runs: it returned 90° when 180° was needed,
                # or 0° for clearly upside-down scans. The 4-variant comparison
                # ("which of A/B/C/D has readable text?") is more reliable
                # because it's a visual comparison, not an angle guess.
                # Cost: ~$0.0003 per document, ~8 docs = ~$0.0024 per report.
                dedicated = _ask_gemini_orientation(source)
            if dedicated is not None and dedicated != 0:
                print(f"Annex orientation: dedicated Gemini call returned {dedicated}° for {Path(source).name}")
                correction = dedicated
            elif dedicated == 0 and correction and correction != 0:
                # Dedicated says original is correct, but OCR said rotate —
                # trust dedicated (it compared 4 images visually).
                print(f"Annex orientation: dedicated Gemini says 0° (original OK), overriding OCR's {correction}° for {Path(source).name}")
                correction = 0
            print(
                f"Annex orientation: file={Path(source).name}, page={page_number}, "
                f"local={local_correction}, gemini={gemini_correction}, "
                f"selected={correction}",
                flush=True,
            )
            if correction:
                print(f"Annex image orientation corrected: file={Path(source).name}, clockwise={correction}")
                image = image.rotate(-correction, expand=True)
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            # DOCX stores every embedded image.  Preserve its aspect ratio but
            # cap dimensions and use JPEG to keep the complete editable report
            # responsive instead of repeatedly embedding multi-megabyte PNGs.
            # Defaults 1280x1800 / q78 for document scans. Analog screenshots
            # pass a quality bump (q92) but no artificial upscale cap: the
            # captured frame is ~900x1600, thumbnail() only ever downscales,
            # so a larger max_size would do nothing.
            image.thumbnail(max_size)
            prepared = Path(source).with_name(f"_annex_upright_{len(temporary) + 1}.jpg")
            image.convert("RGB").save(prepared, format="JPEG", quality=jpeg_quality, optimize=True, progressive=True)
            temporary.append(prepared)
            return prepared
    except Exception as error:
        print("Report source image orientation error: " + str(error))
        return Path(source)


def _make_annex_cover(path, title, subtitle, fonts):
    fn, fb = fonts["n"], fonts["b"]
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    c.setFont(fb, 16)
    c.drawCentredString(w / 2, h - 5 * cm, "ДОДАТОК")
    c.setFont(fb, 12)
    for index, line in enumerate(_wrap(title, 70)):
        c.drawCentredString(w / 2, h - (6.2 + index * 0.55) * cm, line)
    c.setFont(fn, 9)
    y = h - 8.3 * cm
    for line in _wrap(subtitle, 86):
        c.drawCentredString(w / 2, y, line)
        y -= 0.42 * cm
    c.save()


def _make_image_annex(path, image_path, title, fonts):
    fn, fb = fonts["n"], fonts["b"]
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    c.setFont(fb, 11)
    c.drawCentredString(w / 2, h - 1.8 * cm, title)
    try:
        image = ImageReader(str(image_path))
        iw, ih = image.getSize()
        max_w, max_h = w - 3 * cm, h - 5 * cm
        scale = min(max_w / iw, max_h / ih)
        draw_w, draw_h = iw * scale, ih * scale
        c.drawImage(image, (w - draw_w) / 2, (h - draw_h) / 2 - 0.4 * cm, draw_w, draw_h)
    except Exception as error:
        c.setFont(fn, 10)
        c.drawCentredString(w / 2, h / 2, "Не вдалося вставити зображення: " + str(error)[:80])
    c.save()


def _label_source_pdf_page(page, title, fonts):
    """Put an annex label on the same page as a source-PDF scan.

    A separate divider sheet wastes paper and made older reports look broken.
    The narrow white strip is deliberately limited to the upper margin, so the
    appraiser can still read the original scan on that very page.
    """
    try:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        buffer = BytesIO()
        overlay = canvas.Canvas(buffer, pagesize=(width, height))
        overlay.setFillColor(HexColor("#FFFFFF"))
        overlay.rect(0, height - 24, width, 24, fill=1, stroke=0)
        overlay.setFillColor(black)
        overlay.setFont(fonts["b"], 8.5)
        overlay.drawString(18, height - 15, title[:145])
        overlay.save()
        buffer.seek(0)
        page.merge_page(PdfReader(buffer).pages[0])
    except Exception as error:
        # Annexes must never make a ready report fail merely because one
        # uploaded PDF has unusual page geometry or protection.
        print("Report source PDF label error: " + str(error))
    return page


def _append_source_documents(full_path, report, user, fonts):
    """Append original PDFs and image scans after the full report."""
    entries = _document_entries(report, user)
    if not entries:
        return full_path
    output = PdfWriter()
    for page in PdfReader(full_path).pages:
        output.add_page(page)
    temporary = []
    try:
        for index, (source, label, page_orientations) in enumerate(entries, start=1):
            # An image scan is rendered with its description in the page
            # header.  No separate divider page is inserted: one image means
            # one page in the final package.
            if Path(source).suffix.lower() == ".pdf":
                try:
                    # Source PDFs are rasterised before insertion.  This is
                    # intentional: page.rotate() is not stable for scans that
                    # already contain PDF /Rotate metadata.  The raster is
                    # visually checked and corrected once, then placed on an
                    # A4 annex page just like an uploaded JPG or PNG.
                    page_count = len(PdfReader(source).pages)
                    for page_number in range(1, page_count + 1):
                        rendered = _render_pdf_page_for_annex(source, page_number, temporary)
                        if not rendered:
                            # Keep evidence available if Poppler is missing on
                            # a newly configured server. The deployment guide
                            # installs it, so normal production runs always
                            # take the verified raster path above.
                            raise RuntimeError(f"cannot render source page {page_number}")
                        display_image = _prepare_image_for_annex(
                            rendered,
                            page_orientations,
                            temporary,
                            page_number=page_number,
                        )
                        image_pdf = Path(full_path).with_name(
                            f"_annex_pdf_{index}_{page_number}.pdf"
                        )
                        _make_image_annex(
                            image_pdf,
                            display_image,
                            f"Додаток {index}. {label} — сторінка {page_number}",
                            fonts,
                        )
                        temporary.append(image_pdf)
                        for rendered_page in PdfReader(str(image_pdf)).pages:
                            output.add_page(rendered_page)
                    # Every source-PDF page has now been turned into one
                    # visually upright annex page.  Do not import the
                    # original PDF pages as that would reintroduce their
                    # unreliable /Rotate metadata.
                    continue
                except Exception as error:
                    print("Report source PDF insertion error: " + str(error))
            else:
                image_pdf = Path(full_path).with_name(f"_annex_image_{index}.pdf")
                display_image = _prepare_image_for_annex(source, page_orientations, temporary)
                _make_image_annex(image_pdf, display_image, label, fonts)
                temporary.append(image_pdf)
                for page in PdfReader(str(image_pdf)).pages:
                    output.add_page(page)
        with open(full_path, "wb") as stream:
            output.write(stream)
    finally:
        for item in temporary:
            try:
                item.unlink(missing_ok=True)
            except Exception:
                pass
    return full_path

async def generate_word(report, user, analogs):
    doc = Document()
    # Word uses Times New Roman as the official office-document typeface for
    # all text roles, not only for body paragraphs.
    for style_name in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[style_name]
        style.font.name = "Times New Roman"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    s = doc.styles["Normal"]
    s.font.name = "Times New Roman"
    s.font.size = Pt(12)
    s.paragraph_format.space_after = Pt(2)
    s.paragraph_format.space_before = Pt(0)

    for sec in doc.sections:
        sec.left_margin = Cm(3)
        sec.right_margin = Cm(1.5)
        sec.top_margin = Cm(2 + (user.sod_header_offset_mm or 0) / 10)
        sec.bottom_margin = Cm(1.5)

    # Minimal evaluator logo, if the appraiser uploaded one in the profile.
    logo = _profile_logo_path(user)
    if logo:
        try:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.add_run().add_picture(logo, width=Cm(4.2))
            p.paragraph_format.space_after = Pt(2)
        except Exception:
            pass

    # Title
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(user.sod_name or "SOD")
    r.bold = True
    r.font.size = Pt(13)
    p.paragraph_format.space_after = Pt(0)

    if user.sod_address:
        p = doc.add_paragraph(user.sod_address)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(0)
    if user.sod_cert_number:
        p = doc.add_paragraph("Сертифiкат ФДМУ: " + str(user.sod_cert_number or ""))
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_paragraph().paragraph_format.space_after = Pt(6)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("ЗВIТ ПРО ОЦIНКУ МАЙНА")
    r.bold = True
    r.font.size = Pt(15)
    p.paragraph_format.space_after = Pt(4)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("(" + (report.address or "?") + ")")
    r.font.size = Pt(12)
    if "address" in _review_fields(report):
        r.bold = True; r.underline = True
    p.paragraph_format.space_after = Pt(6)

    _add_review_notice_word(doc, report)

    t = doc.add_table(rows=3, cols=2)
    t.style = "Table Grid"
    for i, d in enumerate([["Оцiнювач:", user.full_name or ""], ["Свiдоцтво:", user.cert_number or ""], ["Дата:", datetime.now().strftime("%d.%m.%Y")]]):
        t.rows[i].cells[0].text = d[0]
        t.rows[i].cells[1].text = d[1]
        for c in t.rows[i].cells:
            for pp in c.paragraphs:
                pp.paragraph_format.space_after = Pt(0)
                for rr in pp.runs:
                    rr.font.size = Pt(12)

    # Object
    h = doc.add_heading("1. Об'єкт оцiнки", level=2)
    h.paragraph_format.space_before = Pt(0)
    h.paragraph_format.space_after = Pt(2)

    items = []
    if report.address: items.append(("address", "Адреса: " + report.address))
    if report.area_sqm: items.append(("area_total", "Площа загальна: " + str(report.area_sqm) + " кв.м."))
    al = (report.ocr_raw or {}).get("area_living")
    if al: items.append(("area_living", "Житлова: " + str(al) + " кв.м."))
    if report.rooms: items.append(("rooms", "Кiмнат: " + str(report.rooms)))
    if report.floor: items.append(("floor", "Поверх: " + str(report.floor) + "/" + str(report.total_floors or "?")))
    wm = (report.ocr_raw or {}).get("wall_material")
    if wm: items.append(("wall_material", "Стiни: " + str(wm)))

    for field, item in items:
        p = doc.add_paragraph()
        rr = p.add_run(item)
        if field in _review_fields(report):
            rr.bold = True; rr.underline = True
        p.paragraph_format.space_after = Pt(0)
        for rr in p.runs:
            rr.font.size = Pt(12)

    # Analogs
    h = doc.add_heading("2. Аналоги", level=2)
    h.paragraph_format.space_before = Pt(4)
    h.paragraph_format.space_after = Pt(2)

    if analogs and len(analogs) > 0:
        at = doc.add_table(rows=len(analogs) + 1, cols=8)
        at.style = "Table Grid"
        at.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, txt in enumerate(["N", "Площа", "Цiна грн", "Грн/м2", "Посилання"]):
            c = at.rows[0].cells[i]
            c.text = txt
            for pp in c.paragraphs:
                pp.paragraph_format.space_after = Pt(0)
                for rr in pp.runs:
                    rr.bold = True
                    rr.font.size = Pt(12)
        headers = ["№", "Джерело / район", "Площа", "Кімнати", "Поверх", "Ціна, грн", "грн/м²", "Посилання"]
        for i, txt in enumerate(headers):
            at.rows[0].cells[i].text = txt
            for rr in at.rows[0].cells[i].paragraphs[0].runs:
                rr.bold = True
                rr.font.size = Pt(12)
        for idx, a in enumerate(analogs):
            row = at.rows[idx + 1]
            row.cells[0].text = str(idx + 1)
            row.cells[1].text = (str(a.source or "-") + "\n" + str(a.address or "-"))[:70]
            row.cells[2].text = str(round(a.area_sqm, 1)) + " м²" if a.area_sqm else "-"
            row.cells[3].text = str(a.rooms or "-")
            row.cells[4].text = str(a.floor or "-")
            row.cells[5].text = "{:,.0f}".format(a.price_uah) if a.price_uah else "-"
            row.cells[6].text = "{:,.0f}".format(a.price_per_sqm) if a.price_per_sqm else "-"
            _add_hyperlink(row.cells[7].paragraphs[0], a.url, f"{str(a.source or 'джерело').upper()} №{idx + 1}")
            for c in row.cells:
                for pp in c.paragraphs:
                    pp.paragraph_format.space_after = Pt(0)
                    for rr in pp.runs:
                        rr.font.size = Pt(12)

    # Conclusion
    h = doc.add_heading("3. Висновок", level=2)
    h.paragraph_format.space_before = Pt(4)
    h.paragraph_format.space_after = Pt(2)

    if report.estimated_value:
        p = doc.add_paragraph("Ринкова вартiсть об'єкта оцiнки становить:")
        p.paragraph_format.space_after = Pt(2)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run("{:,.0f} грн".format(report.estimated_value))
        r.bold = True
        r.font.size = Pt(14)
    p = doc.add_paragraph("Оцiнювач: _________________ " + (user.full_name or ""))
    p.paragraph_format.space_before = Pt(6)
    doc.add_paragraph("Дата: " + datetime.now().strftime("%d.%m.%Y"))

    rd = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(rd, exist_ok=True)
    wp = os.path.join(rd, "report.docx")
    doc.save(wp)
    return wp


def _set_cell_width(cell, width_cm):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.first_child_found_in("w:tcW")
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(int(width_cm * 567)))
    tc_w.set(qn("w:type"), "dxa")


async def generate_word_compact(report, user, analogs):
    """Create the readable current Word draft; keep full URLs outside tables."""
    doc = Document()
    for style_name in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[style_name]
        style.font.name = "Times New Roman"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.paragraph_format.space_after = Pt(2)
    normal.paragraph_format.space_before = Pt(0)
    for section in doc.sections:
        section.left_margin = Cm(1.7)
        section.right_margin = Cm(1.7)
        section.top_margin = Cm(1.6 + (user.sod_header_offset_mm or 0) / 10)
        section.bottom_margin = Cm(1.5)

    def paragraph(text="", bold=False, size=12, center=False, space_after=0):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER if center else WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_after = Pt(space_after)
        run = p.add_run(text)
        run.bold = bold
        run.font.name = "Times New Roman"
        run.font.size = Pt(size)
        return p, run

    logo = _profile_logo_path(user)
    if logo:
        try:
            logo_paragraph = doc.add_paragraph()
            logo_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            logo_paragraph.add_run().add_picture(logo, width=Cm(4.2))
            logo_paragraph.paragraph_format.space_after = Pt(2)
        except Exception:
            pass
    paragraph(user.sod_name or "Суб'єкт оціночної діяльності", bold=True, size=13, center=True)
    if user.sod_address:
        paragraph(str(user.sod_address), size=12, center=True)
    if user.sod_cert_number:
        paragraph("Сертифікат СОД / ФДМУ: " + str(user.sod_cert_number), size=12, center=True, space_after=4)
    paragraph("ЗВІТ ПРО ОЦІНКУ МАЙНА", bold=True, size=15, center=True, space_after=3)
    _, address_run = paragraph(report.address or "Адреса уточнюється", size=12, center=True, space_after=5)
    if "address" in _review_fields(report):
        address_run.bold = True
        address_run.underline = True

    _add_review_notice_word(doc, report)
    facts = doc.add_table(rows=3, cols=2)
    facts.style = "Table Grid"
    facts.alignment = WD_TABLE_ALIGNMENT.CENTER
    values = [
        ("Оцінювач", user.full_name or "________________"),
        ("Свідоцтво", user.cert_number or "________________"),
        ("Дата", _report_date(report)),
    ]
    for row, (label, value) in zip(facts.rows, values):
        row.cells[0].text, row.cells[1].text = label, value
        _set_cell_width(row.cells[0], 4.3)
        _set_cell_width(row.cells[1], 13.6)
        for index, cell in enumerate(row.cells):
            for p in cell.paragraphs:
                p.paragraph_format.space_after = Pt(0)
                for run in p.runs:
                    run.font.name = "Times New Roman"
                    run.font.size = Pt(12)
                    if index == 0:
                        run.bold = True

    heading = doc.add_heading("1. Об'єкт оцінки", level=2)
    heading.paragraph_format.space_before = Pt(5)
    heading.paragraph_format.space_after = Pt(1)
    raw = report.ocr_raw or {}
    object_fields = [
        ("address", "Адреса: " + str(report.address or "—")),
        ("area_total", "Загальна площа: " + (f"{float(report.area_sqm):.1f} кв. м" if report.area_sqm else "—")),
        ("area_living", "Житлова площа: " + (str(raw.get("area_living")) + " кв. м" if raw.get("area_living") else "—")),
        ("rooms", "Кількість кімнат: " + str(report.rooms or "—")),
        ("floor", "Поверх / поверховість: " + str(report.floor or "—") + " / " + str(report.total_floors or "—")),
    ]
    for field, text in object_fields:
        _, run = paragraph(text, size=12)
        if field in _review_fields(report):
            run.bold = True
            run.underline = True

    heading = doc.add_heading("2. Порівняльний аналіз", level=2)
    heading.paragraph_format.space_before = Pt(5)
    heading.paragraph_format.space_after = Pt(2)
    if analogs:
        table = doc.add_table(rows=len(analogs) + 1, cols=6)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        layout = table._tbl.tblPr.first_child_found_in("w:tblLayout")
        if layout is None:
            layout = OxmlElement("w:tblLayout")
            table._tbl.tblPr.append(layout)
        layout.set(qn("w:type"), "fixed")
        headers = ["№", "Джерело", "Площа, м²", "Кімнати", "Поверх", "Ціна, грн"]
        widths = [0.7, 2.7, 2.4, 2.2, 2.0, 4.9]
        for index, (header, width) in enumerate(zip(headers, widths)):
            cell = table.rows[0].cells[index]
            cell.text = header
            _set_cell_width(cell, width)
            for run in cell.paragraphs[0].runs:
                run.bold = True
                run.font.name = "Times New Roman"
                run.font.size = Pt(12)
        for index, analog in enumerate(analogs, start=1):
            values = [
                str(index),
                str(analog.source or "—").upper(),
                f"{float(analog.area_sqm):.1f}" if analog.area_sqm else "—",
                str(analog.rooms or "—"),
                str(analog.floor or "—"),
                f"{float(analog.price_uah):,.0f}" if analog.price_uah else "—",
            ]
            row = table.rows[index]
            for col, (text, width) in enumerate(zip(values, widths)):
                row.cells[col].text = text
                _set_cell_width(row.cells[col], width)
                for p in row.cells[col].paragraphs:
                    p.paragraph_format.space_after = Pt(0)
                    for run in p.runs:
                        run.font.name = "Times New Roman"
                        run.font.size = Pt(12)
        paragraph("Повні активні посилання на використані оголошення:", bold=True, size=12, space_after=1)
        for index, analog in enumerate(analogs, start=1):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(1)
            prefix = p.add_run(f"{index}. ")
            prefix.font.name = "Times New Roman"
            prefix.font.size = Pt(12)
            _add_hyperlink(p, str(analog.url or ""), str(analog.url or "—"))

    options = report.report_options or {}
    trade_percent = float(options.get("trade_adjustment_percent") or 0)
    if trade_percent:
        paragraph(
            f"Коригування на торг: -{trade_percent:g}% від цін пропозиції. "
            f"Обґрунтування: {options.get('trade_adjustment_reason') or 'уточнює оцінювач'}.",
            size=12, space_after=2,
        )
    e_certificate_value = float(options.get("e_certificate_value") or 0)
    if e_certificate_value and (getattr(report, "e_certificate_files", None) or []):
        paragraph(
            f"Е-довідка ФДМУ, додана оцінювачем: {e_certificate_value:,.0f} грн. "
            "Остаточне застосування суми перевіряє оцінювач.",
            size=12, space_after=2,
        )

    heading = doc.add_heading("3. Висновок", level=2)
    heading.paragraph_format.space_before = Pt(5)
    heading.paragraph_format.space_after = Pt(2)
    paragraph("Ринкова вартість об'єкта оцінки становить:", size=12, space_after=1)
    amount = float(report.estimated_value or report.selected_value or 0)
    if amount:
        paragraph(f"{amount:,.0f} грн", bold=True, size=14, center=True, space_after=4)
    paragraph("Оцінювач: ______________________________ " + str(user.full_name or ""), size=12)
    paragraph("Дата: " + _report_date(report), size=12)

    report_dir = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(report_dir, exist_ok=True)
    word_path = os.path.join(report_dir, "report.docx")
    doc.save(word_path)
    return word_path


def _configure_word_document(doc, user):
    """Apply the same restrained, readable layout to every DOCX artifact."""
    for style_name in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[style_name]
        style.font.name = "Times New Roman"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.paragraph_format.space_after = Pt(2)
    for section in doc.sections:
        section.left_margin = Cm(1.7)
        section.right_margin = Cm(1.7)
        section.top_margin = Cm(1.6 + (user.sod_header_offset_mm or 0) / 10)
        section.bottom_margin = Cm(1.5)


def _word_paragraph(doc, text="", *, bold=False, size=12, center=False, space_after=0):
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if center else WD_ALIGN_PARAGRAPH.LEFT
    paragraph.paragraph_format.space_after = Pt(space_after)
    run = paragraph.add_run(text)
    run.bold = bold
    run.font.name = "Times New Roman"
    run.font.size = Pt(size)
    return paragraph


async def generate_word_conclusion(report, user):
    """Create the editable notarial conclusion as DOCX, without PDF rendering.

    Mirrors generate_pdf_conclusion's content (intro paragraph, combined
    currency+rate row, amount spelled out in words with a USD equivalent,
    full signature block) — the DOCX version previously only had a bare
    table and a plain number, missing everything a notary/registrar expects
    to see and that the PDF sibling already produced correctly.
    """
    doc = Document()
    _configure_word_document(doc, user)
    logo = _profile_logo_path(user)
    if logo:
        try:
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.add_run().add_picture(logo, width=Cm(4.0))
        except Exception:
            pass
    usd_rate = await _get_usd_rate()
    raw = report.ocr_raw or {}
    identifier = str(raw.get("fdmu_identifier") or raw.get("fdmu_report_id") or "")
    sod = str(user.sod_name or user.full_name or "____________________________")
    sod_cert = str(user.sod_cert_number or "________________")

    _word_paragraph(doc, "ВИСНОВОК ПРО ВАРТІСТЬ МАЙНА", bold=True, size=15, center=True, space_after=5)
    _word_paragraph(doc, "Ідентифікатор за базою ФДМУ №: " + (identifier or "____________________"), bold=True, size=12, center=True, space_after=8)

    intro = (
        f"Суб'єкт оціночної діяльності — {sod}, що діє на підставі "
        f"Сертифіката суб'єкта оціночної діяльності {sod_cert}, виконав незалежну "
        "оцінку майна з метою визначення його ринкової вартості для укладання "
        "цивільно-правових договорів та цілей оподаткування."
    )
    _word_paragraph(doc, intro, size=12, space_after=8)

    object_type = getattr(report.object_type, "value", report.object_type) or "apartment"
    object_names = {"apartment": "квартира", "house": "житловий будинок", "land": "земельна ділянка"}
    description = object_names.get(str(object_type), "об'єкт нерухомого майна")
    if report.rooms and str(object_type) == "apartment":
        description = f"{report.rooms}-кімнатна квартира"
    if report.area_sqm:
        description += f", загальною площею {float(report.area_sqm):.1f} кв. м"
    if raw.get("area_living"):
        description += f", житловою площею {raw.get('area_living')} кв. м"
    if report.address:
        description += f", розташована за адресою: {report.address}"

    owner = str(raw.get("owner_name") or raw.get("owner") or "____________________________")
    executor = sod + f", сертифікат {sod_cert}"
    rows = [
        ("Власник", owner),
        ("Замовник", str(raw.get("customer_name") or owner)),
        ("Виконавець (суб'єкт оціночної діяльності)", executor),
        ("Об'єкт оцінки (у т. ч. місце розташування)", description),
        ("Дата оцінки", _report_date(report)),
        ("Дата обстеження об'єкта", _report_date(report)),
        ("Дата складання звіту", _report_date(report)),
        ("Використаний методичний підхід", "Порівняльний"),
        ("Валюта оцінки, курс НБУ долара США на дату оцінки",
         f"Національна валюта України — гривня. Курс НБУ: 1 дол. США = {usd_rate:.2f} грн."),
    ]
    table = doc.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for row, (label, value) in zip(table.rows, rows):
        row.cells[0].text = label
        row.cells[1].text = value
        _set_cell_width(row.cells[0], 5.2)
        _set_cell_width(row.cells[1], 12.7)
        for index, cell in enumerate(row.cells):
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(0)
                for run in paragraph.runs:
                    run.font.name = "Times New Roman"
                    run.font.size = Pt(12)
                    if index == 0:
                        run.bold = True

    estimated_value = float(report.estimated_value or report.selected_value or 0)
    _word_paragraph(
        doc,
        "На підставі проведених розрахунків Оцінювач робить висновок, що "
        "ринкова вартість об'єкта оцінки становить (з округленням):",
        size=12, space_after=6,
    )
    object_label = description.split(",")[0] if description else "об'єкт нерухомого майна"
    heading = doc.add_paragraph()
    heading_run = heading.add_run(f"Загальна вартість об'єкта оцінки ({object_label})")
    heading_run.bold = True
    heading_run.underline = True
    heading_run.font.name = "Times New Roman"
    heading_run.font.size = Pt(12)
    heading.paragraph_format.space_after = Pt(6)

    if estimated_value:
        usd = estimated_value / usd_rate if usd_rate else 0
        amount_text = (
            f"{estimated_value:,.0f} грн. ({_amount_in_words(estimated_value)}), "
            f"що еквівалентно {usd:,.2f} дол. США за курсом НБУ "
            f"(1 дол. США = {usd_rate:.2f} грн.)."
        )
        _word_paragraph(doc, amount_text, size=12, space_after=10)
    else:
        _word_paragraph(doc, "Вартість визначається оцінювачем після перевірки розрахунків.", size=12, space_after=10)

    _word_paragraph(doc, "")
    signature_rows = [
        ("Експерт-оцінювач", str(user.full_name or "")),
        ("Юридична особа (виконавець)", sod),
        ("Керівник суб'єкта оціночної діяльності", ""),
    ]
    for label, value in signature_rows:
        line = doc.add_paragraph()
        line.paragraph_format.space_after = Pt(10)
        run = line.add_run(f"{label}: ______________________________ {value}")
        run.font.name = "Times New Roman"
        run.font.size = Pt(12)

    report_dir = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "conclusion.docx")
    doc.save(path)
    return path


async def generate_full_word_package(report, user, analogs, include_screenshots=False):
    """Append selected evidence to an editable DOCX package.

    Every supplied scan is rendered/oriented before insertion, so the Word
    package uses the same upright-evidence path as the former full PDF.
    """
    report_dir = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(report_dir, exist_ok=True)
    base_path = str(getattr(report, "word_path", "") or "")
    if not base_path or not os.path.exists(base_path):
        base_path = await generate_word_compact(report, user, analogs)
    doc = Document(base_path)
    _configure_word_document(doc, user)
    temporary = []

    def add_heading(text):
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_before = Pt(8)
        run = paragraph.add_run(text)
        run.bold = True
        run.font.name = "Times New Roman"
        run.font.size = Pt(13)

    def add_image(image_path, label, width=Cm(15.5), max_height_cm=None):
        _word_paragraph(doc, label, bold=True, size=12, space_after=2)
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        try:
            if max_height_cm:
                # Different capture paths (ZenRows' fixed 900x1600 viewport,
                # a direct browser visit, or the HTML-injection fallback)
                # produce images with genuinely different native aspect
                # ratios — one real screenshot came back 557x1800 (ratio
                # 3.23) instead of the assumed 900x1600 (ratio 1.78). A
                # single fixed display width silently assumed the second
                # ratio and overflowed the page for the first, splitting the
                # image onto its own next page and leaving the caption alone
                # on a near-empty one. Reading the actual pixel size and
                # solving for a width that keeps height <= max_height_cm
                # keeps image+caption on one page regardless of which
                # capture path produced this particular file.
                try:
                    with PILImage.open(str(image_path)) as probe:
                        native_w, native_h = probe.size
                    if native_w and native_h:
                        aspect = native_h / native_w
                        fitted_width = Cm(max_height_cm) / aspect
                        if fitted_width < width:
                            width = fitted_width
                except Exception:
                    pass
            paragraph.add_run().add_picture(str(image_path), width=width)
        except Exception as error:
            _word_paragraph(doc, "Не вдалося додати зображення: " + str(error)[:100], size=12)

    try:
        # Package 3 receives the structured expert narrative generated from
        # confirmed document data and object photos. It was persisted in
        # valuation_statistics but previously omitted from the full Word file.
        expert_full = (getattr(report, "valuation_statistics", None) or {}).get("expert_full")
        expert_text = expert_full.get("text") if isinstance(expert_full, dict) else {}
        if isinstance(expert_text, dict):
            sections = (
                ("object_and_condition", "ОБ'ЄКТ ОЦІНКИ ТА ЙОГО СТАН"),
                ("location", "ЛОКАЦІЯ"),
                ("comparative_reasoning", "ОБҐРУНТУВАННЯ ПОРІВНЯЛЬНОГО ПІДХОДУ"),
                ("expert_summary", "ЕКСПЕРТНЕ РЕЗЮМЕ"),
            )
            available_sections = [
                (title, str(expert_text.get(key) or "").strip())
                for key, title in sections
                if str(expert_text.get(key) or "").strip()
            ]
            if available_sections:
                doc.add_page_break()
                add_heading("РОЗШИРЕНИЙ ЕКСПЕРТНИЙ ОПИС ОБ'ЄКТА")
                for title, body in available_sections:
                    _word_paragraph(doc, title, bold=True, size=12, space_after=2)
                    _word_paragraph(doc, body, size=12, space_after=7)

        photo_sources = [
            Path(str(item)) for item in (getattr(report, "object_photo_files", None) or [])
            if Path(str(item)).is_file() and Path(str(item)).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
        entries = _document_entries(report, user)
        has_screenshot_analogs = include_screenshots and any(
            Path(str(getattr(analog, "screenshot_path", "") or "")).is_file() for analog in analogs
        )
        # A page break followed immediately by a heading and nothing else
        # (no photos, no screenshots, no document scans) rendered as a
        # visually blank page with just "ДОДАТКИ ДО ЗВІТУ" at the top — the
        # next section then added its *own* page break, doubling the gap.
        # Only start the annex section when there is something to put in it.
        if photo_sources or has_screenshot_analogs or entries:
            doc.add_page_break()
            add_heading("ДОДАТКИ ДО ЗВІТУ")

        for offset in range(0, len(photo_sources), 6):
            _word_paragraph(doc, "Фотографії об'єкта оцінки", bold=True, size=12, center=True, space_after=4)
            table = doc.add_table(rows=(len(photo_sources[offset:offset + 6]) + 1) // 2, cols=2)
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for local_index, source in enumerate(photo_sources[offset:offset + 6]):
                row, column = divmod(local_index, 2)
                cell = table.cell(row, column)
                cell.text = ""
                paragraph = cell.paragraphs[0]
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                try:
                    upright = _prepare_image_for_annex(source, None, temporary, enable_text_orientation=False)
                    paragraph.add_run().add_picture(str(upright), width=Cm(7.25))
                    caption = cell.add_paragraph(f"Фото {offset + local_index + 1}")
                    caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for run in caption.runs:
                        run.font.name = "Times New Roman"
                        run.font.size = Pt(12)
                except Exception as error:
                    cell.text = "Не вдалося додати фото: " + str(error)[:80]
            if offset + 6 < len(photo_sources):
                doc.add_page_break()

        if include_screenshots:
            first_annex = True
            for index, analog in enumerate(analogs, start=1):
                primary = Path(str(getattr(analog, "screenshot_path", "") or ""))
                if not primary.is_file():
                    continue
                # Don't force a page break before the very first analog when
                # there were no object photos above it — otherwise the
                # "ДОДАТКИ ДО ЗВІТУ" heading is left alone on an otherwise
                # blank page, with the first analog pushed to the next one.
                # Break before every subsequent analog as before.
                if not (first_annex and not photo_sources):
                    doc.add_page_break()
                first_annex = False
                add_heading(f"Аналог {index}. Скріншот оголошення")
                url = str(getattr(analog, "url", "") or "")
                link_paragraph = doc.add_paragraph()
                link_paragraph.paragraph_format.space_after = Pt(3)
                link_paragraph.add_run("\u041f\u043e\u0432\u043d\u0435 \u0430\u043a\u0442\u0438\u0432\u043d\u0435 \u043f\u043e\u0441\u0438\u043b\u0430\u043d\u043d\u044f: ").bold = True
                _add_hyperlink(link_paragraph, url, url)
                details = (
                    f"\u0414\u0436\u0435\u0440\u0435\u043b\u043e: {str(getattr(analog, 'source', '') or '').upper()}; "
                    f"\u043f\u043b\u043e\u0449\u0430: {getattr(analog, 'area_sqm', None) or '—'} \u043a\u0432. \u043c; "
                    f"\u043a\u0456\u043c\u043d\u0430\u0442: {getattr(analog, 'rooms', None) or '—'}; "
                    f"\u043f\u043e\u0432\u0435\u0440\u0445: {getattr(analog, 'floor', None) or '—'}; "
                    f"\u0446\u0456\u043d\u0430: {float(getattr(analog, 'price_uah', 0) or 0):,.0f} \u0433\u0440\u043d."
                )
                _word_paragraph(doc, details, size=12, space_after=4)
                # Crop the RAW capture (`primary`) BEFORE it goes through
                # _prepare_image_for_annex's orientation/thumbnail step --
                # cropping AFTER that step (the previous version of this
                # code) meant a large raw OLX capture (up to ~3400px wide,
                # see "OLX raw ZenRows capture" in the console log) was first
                # downscaled to fit inside 1280x1800, and only THEN cut down
                # to its useful middle ~40% width -- so the kept region ended
                # up rendered at a fraction of its real resolution (e.g.
                # ~512px wide instead of ~1350px), which is exactly the
                # blurry, hard-to-read screenshots seen in a real report.
                # Cropping first means the useful region is thumbnailed on
                # its own, at up to its own full resolution (still capped at
                # 1280x1800 like every other annex image), instead of
                # inheriting a downscale sized for the whole, much larger
                # page. DIM.RIA capture is unaffected by this specific
                # problem (already only 900px wide, under the cap either
                # way), but shares the same crop step for the same reason
                # every per-service crop used to fail silently (see the
                # `Path(temporary)` bug fixed above) -- one crop location.
                _is_olx = str(getattr(analog, "source", "") or "").strip().lower() == "olx"
                cropped_primary = primary
                try:
                    # Smart content-based crop. Instead of fixed percentages
                    # (which broke every time ZenRows returned a different
                    # frame size — 1920x897, 2840x1536, 4576x2438 all seen
                    # on real runs), scan the pixels to find the real
                    # content area: trim empty grey rails on each side,
                    # cut above the content (navbar) and below the content
                    # (ad blocks, footer, viewport padding). Verified on
                    # the images from report 59 — produces crops that
                    # match the user's own template screenshots:
                    #   OLX  960x619 → 594x388 (photo+price+user+specs)
                    #   DIM.RIA 1280x1365 → 1278x744 (whole left pane)
                    from app.services.browser_screenshot_service import smart_crop_listing
                    _left, _top, _right, _bottom = smart_crop_listing(
                        primary, source=("olx" if _is_olx else "dimria")
                    )
                    with PILImage.open(primary) as _shot:
                        _w, _h = _shot.size
                        print(f"Analog screenshot crop input: {_w}x{_h}, source={getattr(analog, 'source', '')!r}")
                        if _right > _left and _bottom > _top:
                            _shot2 = _shot.convert("RGB") if _shot.mode not in ("RGB", "L") else _shot
                            _cropped = _shot2.crop((_left, _top, _right, _bottom))
                            # `temporary` here is the cleanup LIST used throughout this
                            # function (`temporary = []`), not a directory -- the old
                            # "side-trim" code before this fix made the same mistake
                            # (`Path(temporary) / ...`), which is exactly why it always
                            # raised this same TypeError and silently kept the full,
                            # uncropped image every single time, from the very start.
                            # Every other temp file in this file is written next to its
                            # source with `.with_name(...)` instead; do the same here.
                            _cropped_path = primary.with_name(primary.stem + "_cropped.jpg")
                            # quality=95: this is still the RAW-resolution crop, saved
                            # right before _prepare_image_for_annex's own thumbnail/
                            # re-encode step -- keep it as sharp as possible going in.
                            _cropped.save(str(_cropped_path), format="JPEG", quality=95)
                            temporary.append(_cropped_path)
                            cropped_primary = _cropped_path
                            print(f"Analog screenshot cropped to: {_cropped.size[0]}x{_cropped.size[1]}")
                except Exception as _crop_err:
                    print(f"Analog screenshot crop skipped (keeping full capture): {type(_crop_err).__name__}: {_crop_err!r}")
                # Re-encode the cropped frame as a compact JPEG at the
                # same resolution. enable_text_orientation=False skips the
                # Gemini orientation check entirely — screenshots are always
                # upright, calling Gemini on them has rotated readable
                # captures 90° in the past. jpeg_quality=92 keeps the small
                # text on OLX/DIM.RIA cards crisp at Word's 17 cm display
                # width. No max_size override: PIL thumbnail only downscales,
                # and the capture is already ~900×1600, so a 2400×3600 cap
                # would be a no-op.
                display_image = _prepare_image_for_annex(
                    cropped_primary, None, temporary, enable_text_orientation=False,
                    jpeg_quality=92,
                )
                # The composed evidence frame is a fixed 900x1600 portrait
                # image. 15.5 cm scaled to ~27.5 cm tall left no room for the
                # heading/link/details text above it on the same page, so
                # Word pushed the whole image onto its own next page and left
                # the caption alone on a near-empty one. Even 11 cm (~19.6 cm
                # tall) still split across two pages in a real render test;
                # 9.5 cm (~16.9 cm tall) was verified to keep image+caption
                # together on one page.
                # 10.5 cm is the empirically confirmed maximum width that
                # still keeps the image and its caption/link/details text on
                # one page for a 900x1600 evidence frame (tested with a
                # realistic long OLX URL). Doubling the capture height, as
                # requested, is NOT compatible with also keeping everything
                # on one page at this or any wider display width — at double
                # height the safe width drops to ~6 cm, which reads smaller,
                # not larger. Capture height therefore stays 1600; see the
                # ZenRows params in _take_screenshot for that value.
                # width is the maximum for a "normal" 900x1600-shaped
                # capture; max_height_cm makes add_image scale narrower
                # instead when the real captured image is taller/narrower
                # than that (e.g. a fallback capture path's own viewport
                # shape), keeping image+caption on one page either way.
                # Full page width, not height-constrained: OLX's capture is
                # trimmed to its top ~67% at source (see _take_screenshot)
                # specifically so it reads well at this width; DIM.RIA's
                # Cropping is now done at screenshot time (zoom 50% + fixed
                # percentage crop in olx_service/dimria_service/browser_
                # screenshot_service), so no side-trim is needed here.
                # Insert at full available page width; max_height_cm is the
                # safety net for any unusually tall capture.
                add_image(display_image, "Картка оголошення", width=Cm(17), max_height_cm=24)

        if entries:
            # Only force a fresh page here if something (photos/screenshots)
            # was already rendered after "ДОДАТКИ ДО ЗВІТУ" above — otherwise
            # this heading would land right under that one with nothing but
            # whitespace between them, the same blank-page pattern as before.
            if photo_sources or has_screenshot_analogs:
                doc.add_page_break()
            add_heading("Скан-копії документів")
        for source, label, page_orientations in entries:
            source = Path(source)
            if source.suffix.lower() == ".pdf":
                try:
                    page_count = len(PdfReader(str(source)).pages)
                except Exception:
                    page_count = 0
                for page_number in range(1, page_count + 1):
                    rendered = _render_pdf_page_for_annex(source, page_number, temporary)
                    if rendered:
                        upright = _prepare_image_for_annex(rendered, page_orientations, temporary, page_number=page_number)
                        # Same one-page-fit safety net as the analog evidence
                        # screenshots: a scanned document photo can come in
                        # almost any aspect ratio, so cap the display height
                        # instead of trusting one fixed width for every scan.
                        add_image(upright, f"{label}. Сторінка {page_number}", max_height_cm=22)
            else:
                upright = _prepare_image_for_annex(source, page_orientations, temporary)
                add_image(upright, label, max_height_cm=22)

        full_path = os.path.join(report_dir, "full_report.docx")
        doc.save(full_path)
        return full_path
    finally:
        for path in temporary:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass


# ========== PDF ВИСНОВОК ==========

async def generate_pdf_conclusion(report, user):
    """Create the one-page notarial valuation conclusion.

    It intentionally leaves the FDMU identifier blank until the evaluator has
    received the real identifier from the state register.  Signatures and seals
    are likewise never imitated by the service.
    """
    fonts = _reg_fonts()
    fn, fb = fonts["n"], fonts["b"]
    usd_rate = await _get_usd_rate()
    raw = report.ocr_raw or {}
    today = _report_date(report)
    sod = str(user.sod_name or "____________________________")
    sod_cert = str(user.sod_cert_number or "________________")
    owner = str(raw.get("owner_name") or raw.get("owner") or "____________________________")
    identifier = str(raw.get("fdmu_identifier") or raw.get("fdmu_report_id") or "")
    object_kind = str(getattr(report.object_type, "value", report.object_type or "apartment"))
    type_names = {
        "apartment": "квартира", "house": "житловий будинок", "land": "земельна ділянка",
    }
    object_name = type_names.get(object_kind, "об'єкт нерухомого майна")
    object_parts = []
    if object_kind == "apartment" and report.rooms:
        object_parts.append(f"{report.rooms}-кімнатна квартира")
    else:
        object_parts.append(object_name)
    if report.area_sqm:
        object_parts.append(f"загальною площею {float(report.area_sqm):.1f} кв. м")
    living_area = raw.get("area_living")
    if living_area:
        object_parts.append(f"житловою площею {living_area} кв. м")
    if report.address:
        object_parts.append(f"розташована за адресою: {report.address}")
    object_description = ", ".join(object_parts) + "."

    report_dir = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(report_dir, exist_ok=True)
    pdf_path = os.path.join(report_dir, "conclusion.pdf")
    c = canvas.Canvas(pdf_path, pagesize=A4)
    width, height = A4
    left, right = 1.3 * cm, width - 1.3 * cm
    y = height - 1.35 * cm

    c.setStrokeColor(black)
    c.setLineWidth(0.7)
    c.line(left, y, right, y)
    y -= 0.75 * cm
    c.setFont(fb, 15)
    c.drawCentredString(width / 2, y, "Висновок про вартість майна")
    y -= 0.72 * cm
    c.setFont(fb, 11)
    id_label = "Ідентифікатор за базою ФДМУ №: "
    c.drawCentredString(width / 2, y, id_label + (identifier or "________________"))
    y -= 0.68 * cm

    intro = (
        f"Суб'єкт оціночної діяльності — {sod}, що діє на підставі "
        f"Сертифіката суб'єкта оціночної діяльності {sod_cert}, виконав незалежну "
        "оцінку майна з метою визначення його ринкової вартості для укладання "
        "цивільно-правових договорів та цілей оподаткування."
    )
    c.setFont(fn, 8.6)
    for line in _wrap(intro, 112):
        c.drawString(left, y, line)
        y -= 0.37 * cm
    y -= 0.23 * cm
    y = _pdf_review_notice(c, report, y, width)

    executor = sod + f", сертифікат {sod_cert}"
    rows = [
        ("Власник", owner),
        ("Замовник", owner),
        ("Виконавець (суб'єкт оціночної діяльності)", executor),
        ("Об'єкт оцінки (в т. ч. місце розташування)", object_description),
        ("Дата оцінки", today),
        ("Дата обстеження об'єкта", today),
        ("Дата складання звіту", today),
        ("Використаний методичний підхід", "Порівняльний"),
        ("Валюта оцінки, курс НБУ долара США", f"Національна валюта України — гривня. Курс НБУ: 1 дол. США = {usd_rate:.2f} грн."),
    ]
    y = _draw_conclusion_table(c, rows, y, width, fn, fb)
    y -= 0.48 * cm

    c.setFont(fn, 9.5)
    conclusion_text = (
        "На підставі проведених розрахунків оцінювач робить висновок, що "
        "ринкова вартість об'єкта оцінки становить (з округленням):"
    )
    for line in _wrap(conclusion_text, 108):
        c.drawString(left, y, line)
        y -= 0.42 * cm
    y -= 0.2 * cm
    c.setFont(fb, 10.5)
    c.drawString(left, y, f"Загальна вартість об'єкта оцінки ({object_name})")
    c.line(left, y - 0.08 * cm, right - 4.3 * cm, y - 0.08 * cm)
    y -= 0.68 * cm

    estimated_value = float(report.estimated_value or report.selected_value or 0)
    if estimated_value:
        usd = estimated_value / usd_rate if usd_rate else 0
        c.setFont(fn, 9.5)
        amount_text = (
            f"{estimated_value:,.0f} грн. ({_amount_in_words(estimated_value)}), "
            f"що еквівалентно {usd:,.2f} дол. США за курсом НБУ."
        )
        for line in _wrap(amount_text, 108):
            c.drawString(left, y, line)
            y -= 0.42 * cm
        y -= 0.08 * cm
        c.setFont(fb, 12)
        c.drawCentredString(width / 2, y, f"{estimated_value:,.0f} грн")
        y -= 1.15 * cm
    else:
        c.setFont(fb, 10)
        c.drawString(left, y, "Вартість визначається оцінювачем після перевірки розрахунків.")
        y -= 1.05 * cm

    c.setFont(fn, 9.5)
    c.drawString(left, y, "Експерт-оцінювач")
    c.line(7.5 * cm, y - 0.08 * cm, right, y - 0.08 * cm)
    c.drawRightString(right, y, str(user.full_name or ""))
    y -= 0.85 * cm
    c.drawString(left, y, "Юридична особа (виконавець)")
    c.line(7.5 * cm, y - 0.08 * cm, right, y - 0.08 * cm)
    c.drawRightString(right, y, sod)
    y -= 0.85 * cm
    c.drawString(left, y, "Керівник суб'єкта оціночної діяльності")
    c.line(7.5 * cm, y - 0.08 * cm, right, y - 0.08 * cm)
    c.save()
    return pdf_path


async def generate_pdf_conclusion_legacy(report, user):
    fonts = _reg_fonts()
    fn = fonts["n"]
    fb = fonts["b"]
    usd_rate = await _get_usd_rate()

    rd = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(rd, exist_ok=True)
    pdf_path = os.path.join(rd, "conclusion.pdf")

    c = canvas.Canvas(pdf_path, pagesize=A4)
    w, h = A4

    c.setFont(fb, 14)
    c.drawCentredString(w / 2, h - 2.5 * cm, "Висновок про вартiсть майна")

    c.setFont(fn, 9)
    sod = user.sod_name or "SOD"
    cert = user.sod_cert_number or ""
    intro = "Суб'єкт оцiночної дiяльностi - " + sod + ", що дiє на пiдставi Сертифiката " + cert + ", виконав незалежну оцiнку майна з метою визначення ринкової вартостi нерухомого майна, для укладання цивiльно-правових договорiв, для цiлей оподаткування тощо."
    y = h - 3.5 * cm
    y = _pdf_review_notice(c, report, y, w)
    c.setFont(fn, 9)
    for line in _wrap(intro, 90):
        c.drawString(2 * cm, y, line)
        y -= 0.4 * cm

    y -= 0.5 * cm
    owner = str((report.ocr_raw or {}).get("owner_name") or "")
    dt = datetime.now().strftime("%d.%m.%Y")
    ur = "{:.2f}".format(usd_rate)
    obj = ""
    if report.rooms: obj += str(report.rooms) + "-х кiмнатна квартира, "
    if report.area_sqm: obj += "загальною площею " + str(report.area_sqm) + " кв.м., "
    if report.address: obj += "за адресою: " + str(report.address)

    for label, value in [("Власник", owner), ("Замовник", owner), ("Виконавець (СОД)", sod + ", сертифiкат " + cert), ("Об'єкт оцiнки", obj), ("Дата оцiнки", dt), ("Дата обстеження", dt), ("Дата складання звiту", dt), ("Методичний пiдхiд", "Порiвняльний"), ("Валюта, курс НБУ", "Гривня. Курс НБУ долар США = " + ur + " грн.")]:
        c.setFont(fb, 9)
        c.drawString(2 * cm, y, label + ":")
        c.setFont(fn, 9)
        for vl in _wrap(str(value), 52):
            c.drawString(7.5 * cm, y, vl)
            y -= 0.4 * cm
        y -= 0.1 * cm

    y -= 0.4 * cm
    c.setFont(fn, 10)
    for line in _wrap("На пiдставi проведених розрахункiв Оцiнювач робить висновок, що ринкова вартiсть об'єкта оцiнки становить (з округленням):", 85):
        c.drawString(2 * cm, y, line)
        y -= 0.45 * cm

    y -= 0.3 * cm
    c.setFont(fb, 11)
    if report.rooms:
        c.drawCentredString(w / 2, y, "Загальна вартiсть (" + str(report.rooms) + "-х кiмнатна квартира)")
    y -= 0.6 * cm

    if report.estimated_value:
        val = report.estimated_value
        usd = round(val / usd_rate, 2) if usd_rate > 0 else 0
        c.setFont(fn, 10)
        txt = "{:,.2f} грн., що еквiвалентно {:.2f} дол. США по курсу НБУ - 1 дол. США = ".format(val, usd) + ur + " грн."
        for line in _wrap(txt, 85):
            c.drawString(2 * cm, y, line)
            y -= 0.45 * cm
        y -= 0.2 * cm
        c.setFont(fb, 13)
        c.drawCentredString(w / 2, y, "{:,.2f} грн.".format(val))

    y -= 1.5 * cm
    c.setFont(fn, 10)
    c.drawString(2 * cm, y, "Експерт-оцiнювач")
    c.drawString(12 * cm, y, user.full_name or "________________")
    y -= 0.8 * cm
    c.drawString(2 * cm, y, "Юридична особа")
    c.drawString(12 * cm, y, sod)

    c.save()
    return pdf_path


# ========== PDF ПОВНИЙ ПАКЕТ ==========

async def generate_full_pdf_legacy(report, user, analogs, include_screenshots=False):
    """Legacy generator retained only for migration comparison; never call it."""
    fonts = _reg_fonts()
    fn = fonts["n"]
    fb = fonts["b"]
    usd_rate = await _get_usd_rate()

    rd = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(rd, exist_ok=True)
    full_path = os.path.join(rd, "full_report.pdf")

    c = canvas.Canvas(full_path, pagesize=A4)
    w, h = A4
    page_num = [0]

    def new_page(title=""):
        if page_num[0] > 0:
            c.showPage()
        page_num[0] += 1
        # Footer
        c.setFont(fn, 8)
        c.drawCentredString(w / 2, 1 * cm, "Стор. " + str(page_num[0]))
        if title:
            c.setFont(fb, 14)
            c.drawCentredString(w / 2, h - 2.5 * cm, title)
            return h - 3.5 * cm
        return h - 2 * cm

    sod = user.sod_name or "SOD"
    cert = user.sod_cert_number or ""
    dt = datetime.now().strftime("%d.%m.%Y")
    ur = "{:.2f}".format(usd_rate)
    owner = str((report.ocr_raw or {}).get("owner_name") or "")

    # ===== СТОР 1: ТИТУЛЬНА =====
    y = new_page()
    c.setFont(fb, 12)
    c.drawCentredString(w / 2, y, sod)
    y -= 0.6 * cm
    c.setFont(fn, 10)
    if user.sod_address:
        c.drawCentredString(w / 2, y, user.sod_address or "")
        y -= 0.5 * cm
    c.drawCentredString(w / 2, y, "Сертифiкат: " + cert)
    y -= 2 * cm
    c.setFont(fb, 18)
    c.drawCentredString(w / 2, y, "ЗВIТ")
    y -= 0.8 * cm
    c.drawCentredString(w / 2, y, "ПРО ОЦIНКУ МАЙНА")
    y -= 1 * cm
    c.setFont(fn, 12)
    c.drawCentredString(w / 2, y, "(" + (report.address or "?") + ")")
    y -= 2 * cm
    c.setFont(fn, 11)
    c.drawString(3 * cm, y, "Оцiнювач: " + (user.full_name or ""))
    y -= 0.5 * cm
    c.drawString(3 * cm, y, "Дата оцiнки: " + dt)
    y -= 2 * cm
    c.setFont(fb, 13)
    if report.estimated_value:
        c.drawCentredString(w / 2, y, "Вартiсть: {:,.0f} грн".format(report.estimated_value))

    # ===== СТОР 2: ВИСНОВОК =====
    y = new_page("Висновок про вартiсть майна")
    y = _pdf_review_notice(c, report, y, w)
    c.setFont(fn, 9)
    for label, value in [("Власник", owner), ("Замовник", owner), ("Виконавець", sod), ("Об'єкт", (report.address or "")), ("Дата", dt), ("Метод", "Порiвняльний"), ("Курс НБУ", "1 USD = " + ur + " грн")]:
        c.setFont(fb, 9)
        c.drawString(2 * cm, y, label + ":")
        c.setFont(fn, 9)
        c.drawString(6 * cm, y, str(value)[:70])
        y -= 0.5 * cm
    y -= 0.5 * cm
    if report.estimated_value:
        val = report.estimated_value
        usd = round(val / usd_rate, 2)
        c.setFont(fb, 12)
        c.drawCentredString(w / 2, y, "{:,.0f} грн / {:.0f} USD".format(val, usd))
    y -= 1.5 * cm
    c.setFont(fn, 10)
    c.drawString(2 * cm, y, "Оцiнювач: _________________ " + (user.full_name or ""))
    y -= 0.8 * cm
    c.drawString(2 * cm, y, "Керiвник СОД: _________________")

    # ===== СТОР 3-4: КВАЛIФIКАЦIЙНI ДОКУМЕНТИ =====
    y = new_page("Квалiфiкацiйнi документи")
    c.setFont(fn, 11)
    c.drawCentredString(w / 2, y, "Сертифiкат суб'єкта оцiночної дiяльностi")
    y -= 1 * cm
    c.setFont(fn, 10)
    for line in ["СОД: " + sod, "Код ЄДРПОУ: " + (user.sod_edrpou or "ТЕСТ"), "Сертифiкат: " + cert, "Адреса: " + (user.sod_address or "ТЕСТ"), "", "[Мiсце для скану сертифiката СОД]"]:
        c.drawString(3 * cm, y, line)
        y -= 0.5 * cm

    y = new_page("Свiдоцтво оцiнювача")
    c.setFont(fn, 11)
    c.drawCentredString(w / 2, y, "Квалiфiкацiйне свiдоцтво оцiнювача")
    y -= 1 * cm
    c.setFont(fn, 10)
    for line in ["Оцiнювач: " + (user.full_name or "ТЕСТ"), "Свiдоцтво: " + (user.cert_number or "ТЕСТ"), "Спецiалiзацiя: Оцiнка нерухомого майна", "", "[Мiсце для скану свiдоцтва оцiнювача]"]:
        c.drawString(3 * cm, y, line)
        y -= 0.5 * cm

    # ===== СТОР 5-8: ТЕКСТОВА ЧАСТИНА =====
    y = new_page("1. Загальнi вiдомостi")
    c.setFont(fn, 10)
    texts = [
        "Дата оцiнки: " + dt,
        "Мета: визначення ринкової вартостi для купiвлi-продажу.",
        "База оцiнки: ринкова вартiсть (НС №1, НС №2).",
        "Методичний пiдхiд: порiвняльний (п. 58-60 НС №2).",
        "",
    ]
    for t in texts:
        c.drawString(2 * cm, y, t)
        y -= 0.5 * cm

    y -= 0.5 * cm
    c.setFont(fb, 12)
    c.drawString(2 * cm, y, "2. Опис об'єкта оцiнки")
    y -= 0.8 * cm
    c.setFont(fn, 10)
    props = [("Адреса", report.address or ""), ("Площа загальна", str(report.area_sqm or "") + " кв.м."), ("Кiмнат", str(report.rooms or "")), ("Поверх", str(report.floor or "") + "/" + str(report.total_floors or "")), ("Стiни", str((report.ocr_raw or {}).get("wall_material", "")))]
    for label, value in props:
        if value and value.strip() and value != "/":
            c.drawString(2 * cm, y, label + ": " + value)
            y -= 0.45 * cm

    y = new_page("3. Аналiз ринку та пiдбiр аналогiв")
    c.setFont(fn, 10)
    c.drawString(2 * cm, y, "Для визначення вартостi пiдiбрано " + str(len(analogs)) + " аналогiв з вiдкритих джерел.")
    y -= 1 * cm

    # Analogs table
    c.setFont(fb, 9)
    headers = ["N", "Джерело", "Площа", "Цiна грн", "Грн/м2"]
    x_pos = [2 * cm, 3 * cm, 5.5 * cm, 8 * cm, 11 * cm]
    for i, h_text in enumerate(headers):
        c.drawString(x_pos[i], y, h_text)
    y -= 0.15 * cm
    c.line(2 * cm, y, 14 * cm, y)
    y -= 0.4 * cm

    c.setFont(fn, 9)
    for idx, a in enumerate(analogs):
        c.drawString(x_pos[0], y, str(idx + 1))
        c.drawString(x_pos[1], y, str(a.source or "")[:8])
        c.drawString(x_pos[2], y, str(round(a.area_sqm, 1)) if a.area_sqm else "-")
        c.drawString(x_pos[3], y, "{:,.0f}".format(a.price_uah) if a.price_uah else "-")
        c.drawString(x_pos[4], y, "{:,.0f}".format(a.price_per_sqm) if a.price_per_sqm else "-")
        y -= 0.45 * cm

    # Keep the table compact. Full URLs are printed on their own readable page
    # below, rather than squeezing tiny text beneath this table.

    y = new_page("3.1 Посилання на оголошення-аналоги")
    c.setFont(fn, 10)
    c.drawString(2 * cm, y, "Повні активні посилання на оголошення, використані у порівнянні:")
    y -= 0.65 * cm
    for idx, a in enumerate(analogs, start=1):
        c.setFont(fb, 10)
        c.drawString(2 * cm, y, "Аналог " + str(idx) + ":")
        y -= 0.42 * cm
        y = _draw_clickable_url(c, a.url, 2 * cm, y, fn, 10, 72)
        y -= 0.25 * cm

    y = new_page("4. Розрахунок вартостi")
    c.setFont(fn, 10)
    if analogs:
        prices = [a.price_per_sqm for a in analogs if a.price_per_sqm and a.price_per_sqm > 0]
        avg = sum(prices) / len(prices) if prices else 0
        c.drawString(2 * cm, y, "Середня цiна за 1 кв.м: {:,.0f} грн".format(avg))
        y -= 0.6 * cm
        c.drawString(2 * cm, y, "Площа об'єкта: " + str(report.area_sqm or "?") + " кв.м.")
        y -= 0.6 * cm
        if report.estimated_value:
            c.setFont(fb, 12)
            c.drawString(2 * cm, y, "Вартiсть: {:,.0f} грн".format(report.estimated_value))
            y -= 0.8 * cm
            usd = round(report.estimated_value / usd_rate, 2)
            c.setFont(fn, 10)
            c.drawString(2 * cm, y, "Еквiвалент: {:.0f} дол. США (курс НБУ {} грн)".format(usd, ur))
            y -= 0.8 * cm
            if report.range_min and report.range_max:
                c.drawString(2 * cm, y, "Дiапазон: {:,.0f} - {:,.0f} грн".format(report.range_min, report.range_max))

    y = new_page("5. Висновок")
    c.setFont(fn, 10)
    if report.estimated_value:
        conclusion = "На пiдставi проведеного аналiзу, ринкова вартiсть об'єкта - " + (report.address or "") + " - становить {:,.0f} грн.".format(report.estimated_value)
        for line in _wrap(conclusion, 80):
            c.drawString(2 * cm, y, line)
            y -= 0.5 * cm
    y -= 1 * cm
    c.drawString(2 * cm, y, "Оцiнювач: _________________ " + (user.full_name or ""))
    y -= 0.8 * cm
    c.drawString(2 * cm, y, "Дата: " + dt)

    # ===== ДОДАТКИ =====
    # Original PDFs/JPGs/PNGs are appended below; a blank placeholder is not
    # evidence and is therefore deliberately not generated.
    annexes = _document_entries(report, user)
    y = new_page("6. Додатки")
    c.setFont(fn, 10)
    c.drawString(2 * cm, y, "Оригінали завантажених сканкопій додано після основної частини звіту.")
    y -= 0.6 * cm
    c.setFont(fn, 9)
    if annexes:
        for index, (_, label, _) in enumerate(annexes, start=1):
            for line in _wrap(f"{index}. {label}", 88):
                c.drawString(2 * cm, y, line)
                y -= 0.42 * cm
    else:
        c.drawString(2 * cm, y, "Сканкопії не завантажено до цього звіту.")

    # ===== СКРИНШОТИ АНАЛОГIВ =====
    # One A4 sheet per comparable: header/gallery above, characteristics below.
    screenshot_analogs = []
    if include_screenshots:
        for a in analogs[:5]:
            path = Path(str(getattr(a, "screenshot_path", "") or ""))
            details = path.with_name(path.stem + "_details" + path.suffix) if path.name else None
            if path.is_file() and details and details.is_file():
                screenshot_analogs.append((a, path, details))
    for idx, (a, primary_ss, details_ss_path) in enumerate(screenshot_analogs, start=1):
        y = new_page("Аналог " + str(idx + 1) + ". Скріншоти")
        c.setFont(fb, 10)
        c.drawString(2 * cm, y, "Джерело: " + str(a.source or "").upper())
        y -= 0.45 * cm
        c.setFont(fn, 8)
        c.drawString(2 * cm, y, "Повне посилання:")
        y -= 0.32 * cm
        y = _draw_clickable_url(c, a.url, 2 * cm, y, fn, 9.2, 78)
        for line in _wrap("Короткий опис: " + str(a.title or a.address or "Дані оголошення"), 118)[:2]:
            c.drawString(2 * cm, y, line)
            y -= 0.32 * cm
        details = "Адреса: {address}; площа: {area} кв.м; кімнат: {rooms}; поверх: {floor}; ціна: {price:,.0f} грн ({ppsm:,.0f} грн/кв.м).".format(
            address=str(a.address or "не вказано"), area=str(a.area_sqm or "?"), rooms=str(a.rooms or "?"),
            floor=str(a.floor or "?"), price=float(a.price_uah or 0), ppsm=float(a.price_per_sqm or 0),
        )
        for line in _wrap(details, 118):
            c.drawString(2 * cm, y, line)
            y -= 0.32 * cm

        try:
            c.setFont(fb, 8)
            c.drawString(2 * cm, 21.1 * cm, "Скріншот 1 — заголовок, фото та ціна оголошення")
            c.drawImage(str(primary_ss), 2 * cm, 14.2 * cm, width=16.5 * cm, height=6.4 * cm, preserveAspectRatio=True, anchor="c")
            c.drawString(2 * cm, 13.5 * cm, "Скріншот 2 — характеристики та розташування оголошення")
            c.drawImage(str(details_ss_path), 2 * cm, 6.6 * cm, width=16.5 * cm, height=6.4 * cm, preserveAspectRatio=True, anchor="c")
        except Exception as error:
            print("Report screenshot insertion error: " + str(error))
            c.setFont(fn, 10)
            c.drawCentredString(w / 2, 13 * cm, "Не вдалося вставити скріншоти оголошення.")

    c.save()
    return _append_source_documents(full_path, report, user, fonts)


def _wrap(text, mx):
    words = str(text).split()
    lines = []
    cur = ""
    for w in words:
        # Marketplace URLs have no spaces. Split them deterministically so a
        # full saved link is readable and never runs beyond an A4 page edge.
        while len(w) > mx:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:mx])
            w = w[mx:]
        if len(cur) + len(w) + 1 > mx:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


async def generate_full_pdf(report, user, analogs, include_screenshots=False):
    """Create a compact, readable full package without placeholder pages.

    The evaluator's real scans are appended as evidence.  Empty certificate
    placeholders are intentionally omitted: they do not make a report more
    useful and previously made the package look artificially fragmented.
    """
    fonts = _reg_fonts()
    fn, fb = fonts["n"], fonts["b"]
    usd_rate = await _get_usd_rate()
    report_dir = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(report_dir, exist_ok=True)
    full_path = os.path.join(report_dir, "full_report.pdf")
    c = canvas.Canvas(full_path, pagesize=A4)
    width, height = A4
    left, right = 1.45 * cm, width - 1.45 * cm
    page_number = 0

    def page(title):
        nonlocal page_number
        if page_number:
            c.showPage()
        page_number += 1
        c.setStrokeColor(black)
        c.setLineWidth(0.6)
        c.line(left, height - 1.25 * cm, right, height - 1.25 * cm)
        c.setFont(fb, 14)
        c.drawCentredString(width / 2, height - 2.1 * cm, title)
        c.setFont(fn, 9)
        c.drawCentredString(width / 2, 0.9 * cm, f"Стор. {page_number}")
        return height - 2.8 * cm

    def lines(text, x, y, chars=100, font=fn, size=11, leading=0.48 * cm):
        c.setFont(font, size)
        for line in _wrap(text, chars):
            c.drawString(x, y, line)
            y -= leading
        return y

    def value(obj, name, default=""):
        return getattr(obj, name, default) or default

    raw = value(report, "ocr_raw", {}) or {}
    owner = str(raw.get("owner_name") or raw.get("owner") or "________________")
    sod = str(value(user, "sod_name", "") or "________________")
    cert = str(value(user, "sod_cert_number", "") or "________________")
    date = _report_date(report)
    estimated = float(value(report, "estimated_value", 0) or value(report, "selected_value", 0) or 0)
    object_type = str(getattr(value(report, "object_type", "apartment"), "value", value(report, "object_type", "apartment")))
    type_names = {"apartment": "квартира", "house": "житловий будинок", "land": "земельна ділянка"}
    object_name = type_names.get(object_type, "об’єкт нерухомого майна")
    area = value(report, "area_sqm", "")
    rooms = value(report, "rooms", "")
    floor = value(report, "floor", "")
    floors = value(report, "total_floors", "")

    # 1. The conclusion and the service/evaluator data share one readable A4.
    y = page("Висновок про вартість майна")
    intro = (
        f"Суб’єкт оціночної діяльності — {sod}, сертифікат {cert}, "
        "підготував чернетку оцінки ринкової вартості майна за порівняльним підходом."
    )
    y = lines(intro, left, y, 108, fn, 10.5, 0.43 * cm) - 0.12 * cm
    object_text = object_name
    if rooms and object_type == "apartment":
        object_text = f"{rooms}-кімнатна квартира"
    if area:
        object_text += f", загальна площа {float(area):.1f} кв. м"
    if raw.get("area_living"):
        object_text += f", житлова площа {raw.get('area_living')} кв. м"
    if value(report, "address", ""):
        object_text += f", адреса: {value(report, 'address')}"
    rows = [
        ("Власник", owner), ("Замовник", owner), ("Виконавець", sod),
        ("Об’єкт оцінки", object_text), ("Дата оцінки", date),
        ("Використаний підхід", "Порівняльний"),
        ("Валюта та курс НБУ", f"Гривня; 1 USD = {usd_rate:.2f} грн"),
    ]
    y = _draw_conclusion_table(c, rows, y, width, fn, fb) - 0.45 * cm
    c.setFont(fn, 10.5)
    y = lines("За результатами аналізу ринкова вартість об’єкта оцінки становить:", left, y, 106, fn, 10.5, 0.43 * cm)
    c.setFont(fb, 14)
    c.drawCentredString(width / 2, y - 0.25 * cm, f"{estimated:,.0f} грн" if estimated else "Вартість уточнює оцінювач")
    y -= 1.05 * cm
    c.setFont(fn, 10)
    c.drawString(left, y, "Експерт-оцінювач")
    c.line(7.3 * cm, y - 0.08 * cm, right, y - 0.08 * cm)
    c.drawRightString(right, y, str(value(user, "full_name", "")))
    y -= 0.72 * cm
    c.drawString(left, y, "Юридична особа (виконавець)")
    c.line(7.3 * cm, y - 0.08 * cm, right, y - 0.08 * cm)
    c.drawRightString(right, y, sod)

    # 2. Keep the market analysis on the same first sheet.  The former
    # separate, almost empty analysis page made the package harder to read.
    y -= 0.7 * cm
    c.setStrokeColor(lightgrey)
    c.line(left, y, right, y)
    y -= 0.5 * cm
    c.setFont(fb, 12)
    c.drawString(left, y, "Аналіз ринку та розрахунок")
    y -= 0.52 * cm
    properties = [
        f"Адреса: {value(report, 'address', 'не вказано')}",
        f"Загальна площа: {area or '—'} кв. м; кімнат: {rooms or '—'}; поверх: {floor or '—'}/{floors or '—'}.",
        f"Матеріал стін: {raw.get('wall_material') or 'не вказано'}.",
        f"Для порівняння відібрано {len(analogs)} найбільш подібних оголошень з відкритих джерел.",
    ]
    for item in properties:
        y = lines(item, left, y, 110, fn, 11, 0.43 * cm)
    y -= 0.18 * cm
    headers = [(left, "№"), (left + 0.9 * cm, "Джерело / район"), (left + 4.7 * cm, "Площа"),
               (left + 6.5 * cm, "Кім./пов."), (left + 8.8 * cm, "Ціна, грн"), (left + 12.3 * cm, "грн/кв.м")]
    c.setFillColor(HexColor("#EAF2FC"))
    c.rect(left, y - 0.58 * cm, right - left, 0.58 * cm, fill=1, stroke=0)
    c.setFillColor(black)
    c.setFont(fb, 9.5)
    # Numeric captions use the same right edges as their values.  This keeps
    # the compact comparison table visually stable even for six-digit prices.
    for position, (x, label) in enumerate(headers):
        if position == 2:
            c.drawRightString(left + 6.1 * cm, y - 0.37 * cm, label)
        elif position == 3:
            c.drawRightString(left + 8.3 * cm, y - 0.37 * cm, label)
        elif position == 4:
            c.drawRightString(left + 12.0 * cm, y - 0.37 * cm, label)
        elif position == 5:
            c.drawRightString(right, y - 0.37 * cm, label)
        else:
            c.drawString(x, y - 0.37 * cm, label)
    y -= 0.58 * cm
    c.setFont(fn, 10)
    for index, analog in enumerate(analogs[:5], start=1):
        c.setStrokeColor(lightgrey)
        c.line(left, y - 0.54 * cm, right, y - 0.54 * cm)
        c.setFillColor(black)
        c.drawString(left, y - 0.34 * cm, str(index))
        source_place = (str(value(analog, "source", "")) + " " + str(value(analog, "address", ""))).strip()
        c.drawString(left + 0.9 * cm, y - 0.34 * cm, source_place[:31] or "—")
        c.drawString(left + 4.7 * cm, y - 0.34 * cm, f"{float(value(analog, 'area_sqm', 0)):.1f}" if value(analog, "area_sqm", 0) else "—")
        c.drawString(left + 6.5 * cm, y - 0.34 * cm, f"{value(analog, 'rooms', '—')}/{value(analog, 'floor', '—')}")
        c.drawRightString(left + 12.0 * cm, y - 0.34 * cm, f"{float(value(analog, 'price_uah', 0)):,.0f}" if value(analog, "price_uah", 0) else "—")
        c.drawRightString(right, y - 0.34 * cm, f"{float(value(analog, 'price_per_sqm', 0)):,.0f}" if value(analog, "price_per_sqm", 0) else "—")
        y -= 0.68 * cm
    prices = [float(value(item, "price_per_sqm", 0)) for item in analogs if value(item, "price_per_sqm", 0)]
    average = sum(prices) / len(prices) if prices else 0
    y -= 0.18 * cm
    c.setFont(fb, 10.5)
    c.drawString(left, y, "Розрахунок та висновок")
    y -= 0.5 * cm
    calculation = f"Середня ціна у вибірці: {average:,.0f} грн/кв. м. Площа об’єкта: {area or '—'} кв. м. Ринкова вартість: {estimated:,.0f} грн."
    y = lines(calculation, left, y, 110, fn, 11, 0.44 * cm)
    y -= 0.14 * cm
    c.setFont(fb, 10.5)
    c.drawString(left, y, "Повні активні посилання на аналоги")
    y -= 0.48 * cm
    for index, analog in enumerate(analogs[:5], start=1):
        c.setFont(fb, 9.8)
        c.drawString(left, y, f"{index}.")
        y = _draw_clickable_url(c, str(value(analog, "url", "")), left + 0.45 * cm, y, fn, 9.8, 68)
        y -= 0.12 * cm

    # 3. Only a compact annex list precedes actual source documents.
    annexes = _document_entries(report, user)
    if annexes:
        y = page("Додатки до звіту")
        c.setFont(fn, 11)
        c.drawString(left, y, "До повного пакета додано такі оригінали сканкопій:")
        y -= 0.62 * cm
        for number, (_, label, _) in enumerate(annexes, start=1):
            y = lines(f"{number}. {label}", left, y, 105, fn, 11, 0.46 * cm)

    # 4. A screenshot page exists only for a verified pair of captures.
    verified = []
    if include_screenshots:
        for analog in analogs[:5]:
            primary = Path(str(value(analog, "screenshot_path", "")))
            details = primary.with_name(primary.stem + "_details" + primary.suffix) if primary.name else None
            if primary.is_file() and details and details.is_file():
                verified.append((analog, primary, details))
    for index, (analog, primary, details) in enumerate(verified, start=1):
        y = page(f"Аналог {index}. Скріншоти оголошення")
        c.setFont(fb, 10.5)
        c.drawString(left, y, "Джерело: " + str(value(analog, "source", "")).upper())
        y -= 0.45 * cm
        y = _draw_clickable_url(c, str(value(analog, "url", "")), left, y, fn, 9.5, 64)
        y -= 0.12 * cm
        description = (f"Площа: {value(analog, 'area_sqm', '—')} кв. м; кімнат: {value(analog, 'rooms', '—')}; "
                       f"поверх: {value(analog, 'floor', '—')}; ціна: {float(value(analog, 'price_uah', 0)):,.0f} грн.")
        y = lines(description, left, y, 108, fn, 10, 0.4 * cm)
        c.setFont(fb, 10)
        c.drawString(left, y, "Скріншот 1 — фото, заголовок і ціна")
        c.drawImage(str(primary), left, y - 8.45 * cm, width=right - left, height=7.9 * cm, preserveAspectRatio=True, anchor="c")
        y -= 8.9 * cm
        c.setFont(fb, 10)
        c.drawString(left, y, "Скріншот 2 — характеристики та розташування")
        c.drawImage(str(details), left, y - 8.45 * cm, width=right - left, height=7.9 * cm, preserveAspectRatio=True, anchor="c")

    c.save()
    return _append_source_documents(full_path, report, user, fonts)


async def generate_full_pdf_compact(report, user, analogs, include_screenshots=False):
    """Build the current full package without decorative blank pages.

    The package has a one-page conclusion, a compact comparable-analysis page,
    optional evidence pages (two verified images per selected listing) and the
    original object/appraiser documents.  A scan is never preceded by an empty
    cover page.
    """
    fonts = _reg_fonts()
    fn, fb = fonts["n"], fonts["b"]
    rate = await _get_usd_rate()
    report_dir = os.path.join(settings.generated_dir, str(report.user_id), str(report.id))
    os.makedirs(report_dir, exist_ok=True)
    full_path = os.path.join(report_dir, "full_report.pdf")
    c = canvas.Canvas(full_path, pagesize=A4)
    width, height = A4
    left, right = 1.35 * cm, width - 1.35 * cm
    page_number = 0
    expert_temporary = []

    def value(obj, name, fallback=""):
        return getattr(obj, name, fallback) or fallback

    def new_page(title):
        nonlocal page_number
        if page_number:
            c.showPage()
        page_number += 1
        c.setStrokeColor(black)
        c.setLineWidth(0.7)
        c.line(left, height - 1.25 * cm, right, height - 1.25 * cm)
        c.setFont(fb, 14)
        c.drawCentredString(width / 2, height - 2.05 * cm, title)
        c.setFont(fn, 9)
        c.drawCentredString(width / 2, 0.82 * cm, f"Стор. {page_number}")
        return height - 2.75 * cm

    def draw_lines(text, x, y, chars=104, font=fn, size=11, leading=0.43 * cm):
        c.setFont(font, size)
        for line in _wrap(str(text or ""), chars):
            c.drawString(x, y, line)
            y -= leading
        return y

    def draw_expert_section(y, heading, text):
        """Keep the optional narrative readable and never split a heading from it."""
        if not str(text or "").strip():
            return y
        lines = _wrap(str(text or ""), 106)
        required = (len(lines) + 2) * 0.42 * cm
        if y - required < 2.0 * cm:
            y = new_page("Поглиблений експертний опис (продовження)")
        c.setFont(fb, 11)
        c.drawString(left, y, heading)
        return draw_lines(text, left, y - 0.45 * cm, 106, fn, 10.5, 0.42 * cm) - 0.18 * cm

    def draw_object_photo_sheets():
        """Place property photographs two per row in every PDF package."""
        sources = []
        for source in value(report, "object_photo_files", []) or []:
            path = Path(str(source))
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                sources.append(path)
        # Two columns by three rows is the readable practical maximum on A4.
        # Additional images continue on a new sheet instead of becoming tiny
        # thumbnails that cannot show the visible condition of the property.
        for offset in range(0, len(sources), 6):
            y_photo = new_page("Фотографії об'єкта")
            chunk = sources[offset:offset + 6]
            cell_w = (right - left - 0.45 * cm) / 2
            row_count = max(1, (len(chunk) + 1) // 2)
            available_height = y_photo - 1.55 * cm
            cell_h = min(9.5 * cm, (available_height - (row_count - 1) * 0.62 * cm) / row_count)
            for number, source in enumerate(chunk, start=offset + 1):
                position = number - offset - 1
                column, row = position % 2, position // 2
                x = left + column * (cell_w + 0.45 * cm)
                top = y_photo - row * (cell_h + 0.62 * cm)
                c.setFont(fb, 9.5)
                c.drawString(x, top, f"Фото об'єкта {number}")
                try:
                    # Property photos are already camera-oriented via EXIF.
                    # Do not treat a door number or a small text fragment as a
                    # document and rotate a facade/interior photograph.
                    upright = _prepare_image_for_annex(
                        source, None, expert_temporary, enable_text_orientation=False
                    )
                    image = ImageReader(str(upright))
                    iw, ih = image.getSize()
                    max_w, max_h = cell_w, cell_h - 0.38 * cm
                    scale = min(max_w / iw, max_h / ih)
                    draw_w, draw_h = iw * scale, ih * scale
                    c.drawImage(image, x + (cell_w - draw_w) / 2, top - 0.32 * cm - draw_h, draw_w, draw_h, preserveAspectRatio=True, anchor="c")
                except Exception as error:
                    c.setFont(fn, 8)
                    c.drawString(x, top - 0.55 * cm, "Не вдалося підготувати фото: " + str(error)[:55])

    raw = value(report, "ocr_raw", {}) or {}
    logo = _profile_logo_path(user)
    object_type = str(getattr(value(report, "object_type", "apartment"), "value", value(report, "object_type", "apartment")))
    names = {"apartment": "квартира", "house": "житловий будинок", "land": "земельна ділянка"}
    object_name = names.get(object_type, "об'єкт нерухомого майна")
    if object_type == "apartment" and value(report, "rooms"):
        object_name = f"{value(report, 'rooms')}-кімнатна квартира"
    object_description = object_name
    if value(report, "area_sqm"):
        object_description += f", загальною площею {float(value(report, 'area_sqm')):.1f} кв. м"
    if raw.get("area_living"):
        object_description += f", житловою площею {raw.get('area_living')} кв. м"
    if value(report, "address"):
        object_description += f", розташована за адресою: {value(report, 'address')}"
    owner = str(raw.get("owner_name") or raw.get("owner") or "____________________________")
    sod = str(value(user, "sod_name", "") or "____________________________")
    sod_cert = str(value(user, "sod_cert_number", "") or "________________")
    today = _report_date(report)
    amount = float(value(report, "estimated_value", 0) or value(report, "selected_value", 0) or 0)
    expert_full = (value(report, "valuation_statistics", {}) or {}).get("expert_full")

    # A full expert package starts with a restrained title page.  It follows
    # the structure of a professional Ukrainian valuation report without
    # copying another appraiser's form or inventing certification details.
    if isinstance(expert_full, dict):
        y = new_page("Звіт про оцінку нерухомого майна")
        if logo:
            try:
                c.drawImage(logo, width / 2 - 3.4 * cm, y - 1.55 * cm, width=6.8 * cm, height=1.8 * cm,
                            preserveAspectRatio=True, anchor="c", mask="auto")
            except Exception:
                pass
        y -= 3.25 * cm
        c.setFont(fb, 17)
        c.drawCentredString(width / 2, y, "ЗВІТ ПРО ОЦІНКУ")
        y -= 0.72 * cm
        c.setFont(fb, 13)
        c.drawCentredString(width / 2, y, "нерухомого майна")
        y -= 1.05 * cm
        y = draw_lines(object_description, left + 1.1 * cm, y, 82, fn, 12, 0.52 * cm)
        y -= 1.05 * cm
        c.setFont(fn, 11)
        c.drawCentredString(width / 2, y, f"Дата складання: {today}")
        y -= 0.55 * cm
        c.drawCentredString(width / 2, y, f"Суб'єкт оціночної діяльності: {sod}")
        y -= 2.2 * cm
        c.setFont(fn, 9.5)
        c.drawCentredString(width / 2, y, "Чернетка для професійного доопрацювання та підписання оцінювачем")

    # 1. Notarial-style conclusion on exactly one generated page.
    y = new_page("Висновок про вартість майна")
    if logo:
        try:
            c.drawImage(logo, right - 3.1 * cm, y - 0.1 * cm, width=2.8 * cm, height=1.0 * cm, preserveAspectRatio=True, anchor="ne", mask="auto")
        except Exception:
            pass
    y -= 0.35 * cm
    c.setFont(fb, 11)
    identifier = str(raw.get("fdmu_identifier") or raw.get("fdmu_report_id") or "________________")
    c.drawCentredString(width / 2, y, "Ідентифікатор за базою ФДМУ №: " + identifier)
    y -= 0.62 * cm
    intro = (
        f"Суб'єкт оціночної діяльності — {sod}, сертифікат {sod_cert}, підготував "
        "чернетку визначення ринкової вартості майна за порівняльним підходом. "
        "Остаточне рішення та підпис належать оцінювачу."
    )
    y = draw_lines(intro, left, y, chars=108, size=10.2, leading=0.39 * cm) - 0.15 * cm
    y = _pdf_review_notice(c, report, y, width)
    rows = [
        ("Власник", owner),
        ("Замовник", owner),
        ("Виконавець (суб'єкт оціночної діяльності)", sod + ", сертифікат " + sod_cert),
        ("Об'єкт оцінки (в т. ч. місце розташування)", object_description + "."),
        ("Дата оцінки", today),
        ("Дата обстеження об'єкта", today),
        ("Дата складання звіту", today),
        ("Використаний методичний підхід", "Порівняльний"),
        ("Валюта оцінки, курс НБУ долара США", f"Гривня. 1 дол. США = {rate:.2f} грн."),
    ]
    y = _draw_conclusion_table(c, rows, y, width, fn, fb) - 0.42 * cm
    y = draw_lines("На підставі проведених розрахунків ринкова вартість об'єкта оцінки становить:", left, y, 108, fn, 10.2, 0.4 * cm)
    c.setFont(fb, 13.5)
    c.drawCentredString(width / 2, y - 0.2 * cm, f"{amount:,.0f} грн" if amount else "Вартість уточнюється оцінювачем")
    y -= 1.0 * cm
    c.setFont(fn, 10)
    c.drawString(left, y, "Експерт-оцінювач")
    c.line(7.3 * cm, y - 0.08 * cm, right, y - 0.08 * cm)
    c.drawRightString(right, y, str(value(user, "full_name", "")))
    y -= 0.7 * cm
    c.drawString(left, y, "Юридична особа (виконавець)")
    c.line(7.3 * cm, y - 0.08 * cm, right, y - 0.08 * cm)
    c.drawRightString(right, y, sod)

    # 2. Comparable sample, calculation and complete active URLs stay in one
    # readable page.  The value range is intentionally omitted from generated
    # documents: it is a working-control range in the workspace, not a final
    # conclusion by itself.
    y = new_page("Аналіз ринку та порівняльний підхід")
    area = value(report, "area_sqm", "—")
    object_line = (
        f"Об'єкт: {value(report, 'address', 'адреса уточнюється')}. "
        f"Площа: {area} кв. м; кімнат: {value(report, 'rooms', '—')}; "
        f"поверх: {value(report, 'floor', '—')}/{value(report, 'total_floors', '—')}."
    )
    y = draw_lines(object_line, left, y, 106, fn, 11, 0.43 * cm)
    y -= 0.18 * cm
    source_x = left + 0.8 * cm
    area_right = left + 4.6 * cm
    rooms_right = left + 7.0 * cm
    price_right = left + 11.75 * cm
    ppsm_right = right
    headers = [(left, "№"), (source_x, "Джерело"), (left + 3.4 * cm, "Площа"),
               (left + 5.2 * cm, "Кімн./пов."), (left + 8.0 * cm, "Ціна, грн"), (left + 12.25 * cm, "грн/кв. м")]
    c.setFillColor(HexColor("#EAF2FC"))
    c.rect(left, y - 0.58 * cm, right - left, 0.58 * cm, fill=1, stroke=0)
    c.setFillColor(black)
    c.setFont(fb, 10)
    for position, (x, label) in enumerate(headers):
        if position == 2:
            c.drawRightString(area_right, y - 0.37 * cm, label)
        elif position == 3:
            c.drawRightString(rooms_right, y - 0.37 * cm, label)
        elif position == 4:
            c.drawRightString(price_right, y - 0.37 * cm, label)
        elif position == 5:
            c.drawRightString(ppsm_right, y - 0.37 * cm, label)
        else:
            c.drawString(x, y - 0.37 * cm, label)
    y -= 0.58 * cm
    c.setFont(fn, 10.5)
    price_per_sqm = []
    for index, analog in enumerate(analogs[:5], start=1):
        c.setStrokeColor(lightgrey)
        c.line(left, y - 0.6 * cm, right, y - 0.6 * cm)
        source = str(value(analog, "source", "—")).upper()
        c.setFillColor(black)
        c.drawString(left, y - 0.37 * cm, str(index))
        # Address is deliberately not placed in this narrow column: it used
        # to flow into the square-metre column and visually shift the table.
        # Full active links and each card's details are printed below.
        c.drawString(source_x, y - 0.37 * cm, source[:14])
        analog_area = value(analog, "area_sqm", 0)
        c.drawRightString(area_right, y - 0.37 * cm, f"{float(analog_area):.1f}" if analog_area else "—")
        c.drawRightString(rooms_right, y - 0.37 * cm, f"{value(analog, 'rooms', '—')}/{value(analog, 'floor', '—')}")
        price = float(value(analog, "price_uah", 0) or 0)
        ppsm = float(value(analog, "price_per_sqm", 0) or 0)
        c.drawRightString(price_right, y - 0.37 * cm, f"{price:,.0f}" if price else "—")
        c.drawRightString(ppsm_right, y - 0.37 * cm, f"{ppsm:,.0f}" if ppsm else "—")
        if ppsm:
            price_per_sqm.append(ppsm)
        y -= 0.68 * cm
    median_ppsm = sorted(price_per_sqm)[len(price_per_sqm) // 2] if price_per_sqm else 0
    y -= 0.25 * cm
    c.setFont(fb, 11)
    c.drawString(left, y, "Розрахунок вартості")
    y -= 0.45 * cm
    calculation = (
        f"Медіана відібраної вибірки: {median_ppsm:,.0f} грн/кв. м. "
        f"Загальна площа об'єкта: {area} кв. м. "
        f"Підсумкова вартість: {amount:,.0f} грн."
    )
    y = draw_lines(calculation, left, y, 106, fn, 11, 0.42 * cm)
    options = value(report, "report_options", {}) or {}
    trade_percent = float(options.get("trade_adjustment_percent") or 0)
    if trade_percent:
        y = draw_lines(
            f"Коригування на торг: -{trade_percent:g}% від цін пропозиції. "
            f"Обґрунтування: {options.get('trade_adjustment_reason') or 'уточнює оцінювач'}.",
            left, y - 0.08 * cm, 106, fn, 10.5, 0.4 * cm,
        )
    e_certificate_value = float(options.get("e_certificate_value") or 0)
    if e_certificate_value and (value(report, "e_certificate_files", []) or []):
        y = draw_lines(
            f"Е-довідка ФДМУ, додана оцінювачем: {e_certificate_value:,.0f} грн. "
            "Робочий діапазон у кабінеті визначено від цієї суми ±25%; остаточне рішення приймає оцінювач.",
            left, y - 0.08 * cm, 106, fn, 10.5, 0.4 * cm,
        )
    y -= 0.12 * cm
    c.setFont(fb, 10.5)
    c.drawString(left, y, "Повні активні посилання на аналоги")
    y -= 0.45 * cm
    for index, analog in enumerate(analogs[:5], start=1):
        c.setFont(fb, 10)
        c.drawString(left, y, f"{index}.")
        y = _draw_clickable_url(c, str(value(analog, "url", "")), left + 0.45 * cm, y, fn, 10, 70)
        y -= 0.07 * cm

    # 3. The optional expert mode follows the content structure of a full
    # appraisal package but only uses recognised documents, uploaded photos,
    # selected comparables and an optionally supplied map.
    if isinstance(expert_full, dict):
        expert_text = expert_full.get("text") or {}
        evidence = expert_full.get("evidence") or {}
        y = new_page("Поглиблений експертний опис")
        y = draw_expert_section(y, "1. Об'єкт та видимий стан", expert_text.get("object_and_condition", ""))
        y = draw_expert_section(y, "2. Локація", expert_text.get("location", ""))
        # A supplied map is included later as an image annex.  Do not expose
        # a raw search URL or narrate the absence of map data here.
        y = draw_expert_section(y, "3. Обґрунтування порівняльного підходу", expert_text.get("comparative_reasoning", ""))
        y = draw_expert_section(y, "4. Експертне резюме", expert_text.get("expert_summary", ""))
    # Object photographs are evidence in all three packages.  Keep a maximum
    # of six photographs on each A4 sheet (two columns, three rows) rather
    # than making the basic/evidence package emit one page for every photo.
    # _prepare_image_for_annex applies the text-direction check here as well,
    # so the same upright-orientation rule is used for every package.
    draw_object_photo_sheets()

    # 4. List the actual attached sources once; the following pages contain
    # the originals themselves.  There are no empty 'ДОДАТОК' divider pages.
    annexes = _document_entries(report, user)
    if annexes:
        y = new_page("Додатки до повного пакета")
        c.setFont(fn, 11)
        c.drawString(left, y, "До пакета додано оригінали завантажених документів і фотографій:")
        y -= 0.62 * cm
        for number, (_, label, _) in enumerate(annexes, start=1):
            if y < 2.0 * cm:
                y = new_page("Додатки до повного пакета (продовження)")
            y = draw_lines(f"{number}. {label}", left, y, 105, fn, 11, 0.45 * cm)

    # 5. Every selected listing has one validated portrait evidence frame.
    # OLX's gallery/price and characteristics are composed before this point;
    # DIM.RIA already provides both in one verified frame.  Failed captures
    # are never inserted as evidence.
    if include_screenshots:
        for index, analog in enumerate(analogs[:5], start=1):
            primary = Path(str(value(analog, "screenshot_path", "")))
            if not primary.is_file():
                continue
            y = new_page(f"Аналог {index}. Скріншоти оголошення")
            c.setFont(fb, 10.5)
            c.drawString(left, y, "Джерело: " + str(value(analog, "source", "—")).upper())
            y -= 0.42 * cm
            y = _draw_clickable_url(c, str(value(analog, "url", "")), left, y, fn, 9.7, 72)
            description = (
                f"Площа: {value(analog, 'area_sqm', '—')} кв. м; кімнат: {value(analog, 'rooms', '—')}; "
                f"поверх: {value(analog, 'floor', '—')}; ціна: {float(value(analog, 'price_uah', 0) or 0):,.0f} грн."
            )
            y = draw_lines(description, left, y - 0.05 * cm, 108, fn, 10.5, 0.4 * cm)
            c.setFont(fb, 10)
            c.drawString(left, y, "Скріншот 1 — фото, заголовок і ціна")
            # A 900x1600 evidence frame is deliberately placed tall, not
            # stretched wide.  This keeps both the gallery/price and the
            # lower characteristic block readable on one A4 page.
            c.drawImage(
                str(primary), left, y - 19.4 * cm,
                width=right - left, height=19.0 * cm,
                preserveAspectRatio=True, anchor="c",
            )

    c.save()
    try:
        return _append_source_documents(full_path, report, user, fonts)
    finally:
        for item in expert_temporary:
            try:
                item.unlink(missing_ok=True)
            except Exception:
                pass
