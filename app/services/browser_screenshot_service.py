"""Local Chromium screenshots for report evidence.

Creates a verified listing-header/gallery capture and, where needed, a second
details capture.  DIM.RIA can use one complete frame; OLX normally uses two.
The feature is optional so normal analogue search still works without a browser.
"""
from pathlib import Path
import asyncio
import re
from app.core.config import settings


# One process can use hundreds of MB.  Extra report jobs wait here instead of
# exhausting RAM and bringing down the API.  A worker queue can replace this
# in a later scaling phase without changing callers.
_screenshot_slots = asyncio.Semaphore(max(1, settings.browser_screenshot_concurrency))


def _compose_olx_evidence(primary: Path, details: Path) -> bool:
    """Create one readable 900x1600 OLX evidence image.

    The top viewport contains photo/title/price; the controlled lower
    viewport contains area, rooms and floor.  The report should not shrink two
    unrelated screenshots onto one A4 page.  Instead, use their useful bands
    to make one portrait document-like frame that remains readable at A4.
    """
    try:
        from PIL import Image

        with Image.open(primary) as top_source, Image.open(details) as bottom_source:
            width, height = 900, 1600
            top = top_source.convert("RGB")
            bottom = bottom_source.convert("RGB")
            top = top.resize((width, int(top.height * width / top.width)))
            bottom = bottom.resize((width, int(bottom.height * width / bottom.width)))
            top_height = min(930, top.height)
            bottom_height = height - top_height - 8
            # The details landmark is deliberately near the upper third of
            # the second viewport.  This keeps the property characteristics,
            # not the seller/footer, in the final evidence image.
            bottom_start = max(0, min(bottom.height - bottom_height, int(bottom.height * 0.18)))
            canvas = Image.new("RGB", (width, height), "white")
            canvas.paste(top.crop((0, 0, width, top_height)), (0, 0))
            canvas.paste(bottom.crop((0, bottom_start, width, bottom_start + bottom_height)), (0, top_height + 8))
            canvas.save(primary, format="PNG", optimize=True)
        return primary.is_file() and primary.stat().st_size > 2048
    except Exception as error:
        print("OLX evidence composition error: " + str(error))
        return False


async def _visible_listing_frame(page, *, require_details: bool) -> bool:
    """Confirm that the *viewport* contains evidence, not merely page text.

    OLX may return a technically successful page with the selected advert's
    identifier in the HTML, while the viewport is an author list, a cookie
    placeholder or a lazy-loaded blank card.  Such a file is not suitable for
    an appraisal annex.  The check deliberately runs just before each capture.
    """
    try:
        result = await page.evaluate(
            """(requireDetails) => {
                const inView = (node) => {
                    if (!node) return false;
                    const r = node.getBoundingClientRect();
                    return r.width > 4 && r.height > 4 && r.bottom > 20 && r.top < window.innerHeight - 12;
                };
                const textInView = (pattern) => [...document.querySelectorAll('h1,h2,h3,p,span,div,li')]
                    .some((node) => {
                        const text = (node.innerText || '').trim();
                        return text.length > 0 && text.length < 220 && pattern.test(text) && inView(node);
                    });
                const visiblePhoto = [...document.images].some((image) => {
                    const r = image.getBoundingClientRect();
                    return image.complete && image.naturalWidth > 180 && r.width > 190 && r.height > 145
                        && r.bottom > 20 && r.top < window.innerHeight - 12;
                });
                const title = [...document.querySelectorAll('h1,h2')].some(inView);
                const price = textInView(/(?:\\d[\\d\\s]{2,}\\s*(?:грн|₴|\\$)|\\$\\s*\\d)/i);
                const details = textInView(/(?:площа|кв\\.?\\s*м|кімнат|поверх|кімн\\.?)/i);
                return { title, price, visiblePhoto, details, valid: requireDetails ? details : (title && price && visiblePhoto) };
            }""",
            require_details,
        )
        if not result.get("valid"):
            print("Browser screenshot frame is incomplete: " + str(result))
            return False
        return True
    except Exception as error:
        print("Browser screenshot frame validation error: " + str(error))
        return False


async def take_browser_screenshots(
    url: str,
    save_path: str,
    source_html: str | None = None,
    single_frame: bool = False,
    extra_wait_ms: int = 0,
) -> bool:
    """Make verified evidence screenshots of one confirmed listing.

    ``source_html`` is the listing markup obtained through the configured
    scraper.  OLX frequently presents the VPS IP with an anti-bot or an
    author's-page redirect, even though the scraper has already returned the
    correct listing markup used by the parser.  Rendering that verified markup
    locally keeps the screenshot tied to the exact selected URL and avoids a
    second, unverified OLX visit.

    ``extra_wait_ms`` adds on top of the base 2.2s wait below — DIM.RIA's
    photo gallery needs noticeably longer than that to finish loading.
    """
    try:
        async with _screenshot_slots:
            return await _take_browser_screenshots(
                url, save_path, source_html=source_html, single_frame=single_frame,
                extra_wait_ms=extra_wait_ms,
            )
    except Exception as exc:
        print("Browser screenshot queue error: " + str(exc))
        return False


async def _take_browser_screenshots(
    url: str,
    save_path: str,
    source_html: str | None = None,
    single_frame: bool = False,
    extra_wait_ms: int = 0,
) -> bool:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Browser screenshot unavailable: playwright is not installed")
        return False

    primary = Path(save_path)
    details = primary.with_name(primary.stem + "_details" + primary.suffix)
    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                # The current system service runs as root. This flag is needed
                # until the service is moved to a dedicated unprivileged user.
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = await browser.new_context(
                # Portrait evidence: one frame must show the listing gallery,
                # heading/price and the first characteristics, rather than a
                # shallow desktop strip that is unreadable in the PDF.
                # 1800x2160 — twice the pixel density of the earlier 900x1600,
                # matched to the ZenRows path (ZenRows window_height caps at
                # 2160). zoom 0.5 is applied via JS below, so the browser
                # still renders the page as if it were 3600 CSS px wide,
                # which keeps the listing fitting inside one portrait frame
                # while the captured pixels are ~2x crisper in Word at 17cm.
                viewport={"width": 1800, "height": 2160},
                device_scale_factor=1,
                locale="uk-UA",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            snapshot_mode = bool(source_html)
            if snapshot_mode:
                # ``<base>`` preserves image and stylesheet URLs when the
                # scraper response is placed into an about:blank page.
                markup = re.sub(
                    r"<head(\\s[^>]*)?>",
                    lambda item: item.group(0) + '<base href="' + url.replace('"', '%22') + '">',
                    source_html,
                    count=1,
                    flags=re.IGNORECASE,
                )
                if "<base " not in markup.casefold():
                    markup = '<base href="' + url.replace('"', '%22') + '">' + markup
                await page.set_content(markup, wait_until="domcontentloaded", timeout=60000)
                print("Browser screenshot source: scraper HTML for " + url)
            else:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    # OLX often keeps analytics requests open. The rendered page
                    # can still be usable after this bounded wait.
                    pass
                print("Browser screenshot source: direct page for " + url)
            await page.wait_for_timeout(2200)
            if extra_wait_ms > 0:
                # DIM.RIA's photo gallery needs noticeably more than the base
                # 2.2s to finish loading — requested as "more than 2-5s total".
                await page.wait_for_timeout(extra_wait_ms)
            # Cookie banners otherwise conceal the title/photo section and
            # make an evidence image unusable.
            for label in ("Прийняти", "Приймаю", "Accept", "Дозволити всі"):
                try:
                    await page.get_by_text(label, exact=False).first.click(timeout=800)
                    break
                except Exception:
                    pass
            body_text = (await page.locator("body").inner_text()).lower()
            # A normal OLX card contains a lower-page "all listings by this
            # author" section.  It is not evidence of an author profile, so
            # never reject a valid card merely because that phrase is present.
            # The requested stable listing ID below is the actual guard.
            blocked_markers = (
                "403 error", "request could not be satisfied", "access denied",
                "доступ заборонено", "captcha", "temporarily unavailable",
            )
            # OLX may canonicalise the extension or add a query string after
            # navigation. Compare only its stable listing ID, not the full URL.
            match = re.search(r"\bID[A-Za-z0-9]+", url)
            requested_id = match.group(0) if match else ""
            if snapshot_mode and requested_id and requested_id.casefold() not in source_html.casefold():
                print("Browser screenshot snapshot does not contain listing ID: " + url)
                return False
            if any(marker in body_text for marker in blocked_markers) or (
                not snapshot_mode and requested_id and requested_id not in page.url
            ):
                print("Browser screenshot is not a listing page: " + url)
                return False
            # Preserve the price panel.  OLX places it inside an ``aside`` on
            # some desktop layouts, so hiding every aside produced screenshots
            # of the description without a photo or price.
            await page.add_style_tag(content="""
                header, footer, [role='banner'], [data-testid*='banner'],
                [data-testid*='advert'], [data-testid*='ad-'], .cookie, [class*='cookie'] { display: none !important; }
                body { overflow-x: hidden !important; }
            """)
            try:
                # Do not take evidence while OLX still shows a skeleton.  A
                # listing title and a visible price marker must both exist.
                await page.wait_for_function("""
                    () => {
                        const text = document.body ? document.body.innerText : '';
                        const hasTitle = !!document.querySelector('h1') || /прода[єе]ться|продаж/i.test(text);
                        const hasPrice = /(?:\\d[\\d\\s]{2,}\\s*(?:грн|₴|\\$)|\\$\\s*\\d)/i.test(text);
                        return hasTitle && hasPrice;
                    }
                """, timeout=15000)
            except Exception:
                print("Browser screenshot: OLX listing title/price was not ready: " + url)
                return False
            try:
                # A loading spinner in place of the actual gallery photo was
                # observed on a real DIM.RIA capture despite this check
                # passing — the >180x120 threshold is small enough that a
                # site logo or a small icon elsewhere on the page can satisfy
                # it while the real (much larger) gallery photo is still
                # spinning. Raised well above typical icon/logo size so this
                # can only be satisfied by an actual loaded photo, and the
                # wait itself extended to give a slow gallery image more room.
                await page.wait_for_function("""
                    () => [...document.images].some((image) => {
                        const rect = image.getBoundingClientRect();
                        return image.complete && image.naturalWidth > 400
                            && image.naturalHeight > 300
                            && rect.width > 300 && rect.height > 220;
                    })
                """, timeout=6000)
            except Exception:
                print("Browser screenshot: OLX gallery image was not ready: " + url)
                return False
            # For OLX save one original, full-height page.  The evidence is
            # never composed from cropped fragments: gallery, title, price
            # and characteristics remain available for later verification.
            if single_frame:
                await page.evaluate("window.scrollTo(0, 0)")
                # Zoom 50% + cookie hiding via CSS (same approach as ZenRows)
                try:
                    await page.evaluate("""
                        () => {
                            document.body.style.zoom = '0.5';
                            const css = '[data-testid="cookies-bar"],[class*="cookie"],[class*="Cookie"],[id*="cookie"],[class*="consent"],[class*="Consent"],[class*="gdpr"]{display:none !important;visibility:hidden !important;height:0 !important;}';
                            const s = document.createElement('style');
                            s.innerHTML = css;
                            document.head.appendChild(s);
                            // Also try clicking cookie accept button
                            const patterns = ['дозволити', 'прийняти', 'погоджу', 'accept', 'згод'];
                            const candidates = [...document.querySelectorAll('button, a, [role="button"]')];
                            const match = candidates.find((el) => {
                                const text = (el.textContent || '').trim().toLowerCase();
                                return text.length < 60 && patterns.some((p) => text.includes(p));
                            });
                            if (match) match.click();
                        }
                    """)
                    # Empirically confirmed via test_dimria_shots_v4.py:
                    # 4000ms after zoom reliably lets the DIM.RIA gallery
                    # finish loading. 1500ms produced ~78KB blank captures
                    # on a cold-start Chromium.
                    await page.wait_for_timeout(4000)
                except Exception:
                    pass
                # Viewport screenshot (not full_page): 900x1600 is the target
                # frame. full_page produced tall unpredictable portraits that
                # A4-rendered poorly (title + link + huge image split across
                # 2 Word pages). See test_dimria_shots_v4.py results.
                await page.screenshot(path=str(primary), full_page=False, timeout=60000)
                # Retry once for suspiciously small captures. A cold Chromium
                # can save a ~60-80 KB blank/spinner-only frame that passes
                # the >1024 B guard but is unusable as evidence. A single
                # extra wait + reshoot fixes it (~2.5s cost) without falling
                # through to the paid ZenRows fallback in dimria_service.
                try:
                    if primary.stat().st_size < 150 * 1024:
                        print(
                            "Browser screenshot too small ("
                            + str(primary.stat().st_size)
                            + " B), retrying: " + url
                        )
                        await page.wait_for_timeout(2500)
                        await page.screenshot(
                            path=str(primary), full_page=False, timeout=60000
                        )
                except Exception as retry_error:
                    print("Browser screenshot retry skipped: " + str(retry_error))
                await context.close()
                await browser.close()
                if not (primary.exists() and primary.stat().st_size > 1024):
                    return False
                # Cropping used to happen right here too (OLX 30/70/7/93,
                # DIM.RIA 0/5/100/85), inside a try/except that silently kept
                # the full, uncropped capture on any crop failure -- the same
                # pattern that was found in olx_service.py's ZenRows path and
                # is the leading suspect for full, uncropped pages reaching
                # real reports. As a test, this capture is now saved as-is
                # (zoom 0.5 only); the percentage crop is applied once,
                # centrally, in report_generator.py at document-assembly
                # time, the same single crop step used for every analog
                # image regardless of which method captured it.
                try:
                    from PIL import Image as _PILImage
                    with _PILImage.open(primary) as _shot:
                        print(f"Browser screenshot raw capture: {_shot.size[0]}x{_shot.size[1]}: {url}")
                except Exception as probe_error:
                    print(f"Browser screenshot raw capture could not be read for size: {type(probe_error).__name__}: {probe_error!r}: {url}")
                return True

            # Position the first frame slightly below the page top: it keeps
            # the last gallery photos, heading and price in one readable A4
            # frame instead of spending half the image on site navigation.
            await page.evaluate("""
                () => {
                    const heading = document.querySelector('h1');
                    const maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
                    const headingTop = heading ? window.scrollY + heading.getBoundingClientRect().top : 0;
                    window.scrollTo({top: Math.max(0, Math.min(maxScroll, headingTop - window.innerHeight * 0.35)), behavior: 'instant'});
                }
            """)
            await page.wait_for_timeout(750)
            if not await _visible_listing_frame(page, require_details=False):
                print("Browser screenshot primary frame is not a visible listing: " + url)
                return False
            # A viewport image is intentionally used rather than a full main
            # element: full-page captures become unreadable when placed in an
            # A4 evidence sheet.
            await page.screenshot(path=str(primary), full_page=False, timeout=30000)

            # Locate the visible characteristics block instead of blindly
            # moving into a seller/author section.  Listing pages differ by
            # device layout, while these labels are stable in Ukrainian.
            await page.evaluate("""
                () => {
                    const maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
                    const labels = [...document.querySelectorAll('h1,h2,h3,p,span,div,li')];
                    const candidate = labels.find((node) => {
                        const text = (node.innerText || '').trim();
                        const r = node.getBoundingClientRect();
                        return text.length > 0 && text.length < 180
                            && /(?:площа|кв\\.?\\s*м|кімнат|поверх|характеристики)/i.test(text)
                            && r.top > window.innerHeight * 0.20;
                    });
                    const nextTop = candidate
                        ? window.scrollY + candidate.getBoundingClientRect().top - window.innerHeight * 0.24
                        : window.scrollY + Math.round(window.innerHeight * 0.55);
                    window.scrollTo({top: Math.max(0, Math.min(maxScroll, nextTop)), behavior: 'instant'});
                }
            """)
            # The second image is taken only after the characteristics block
            # has settled following a short controlled scroll.
            await page.wait_for_timeout(950)
            if not await _visible_listing_frame(page, require_details=True):
                print("Browser screenshot details frame has no visible characteristics: " + url)
                return False
            await page.screenshot(path=str(details), full_page=False, timeout=30000)
            if not _compose_olx_evidence(primary, details):
                return False
            await context.close()
            await browser.close()
        return primary.exists() and primary.stat().st_size > 1024 and details.exists() and details.stat().st_size > 1024
    except Exception as exc:
        print("Browser screenshot error: " + str(exc))
        for path in (primary, details):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return False
