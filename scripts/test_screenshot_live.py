"""Live test of ZenRows screenshot params + smart_crop_listing.

Run on the server (where outbound to api.zenrows.com is allowed):
  cd /opt/ocinka && ./venv/bin/python scripts/test_screenshot_live.py

Produces /tmp/zr_test/*.jpg so you can look at the raw and cropped frames
side by side before deciding what to deploy.

Reads ZENROWS_API_KEY from /opt/ocinka/.env — no hardcoded keys.
"""
import os, sys, json, pathlib
sys.path.insert(0, '/opt/ocinka')

from PIL import Image
import httpx
from app.services.browser_screenshot_service import smart_crop_listing

# Load .env
env_path = pathlib.Path('/opt/ocinka/.env')
for line in env_path.read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"\''))

API_KEY = os.environ.get('ZENROWS_API_KEY')
if not API_KEY:
    sys.exit('ZENROWS_API_KEY missing in .env')

URLS = [
    ("olx", "https://www.olx.ua/d/uk/obyavlenie/prodatsya-budinok-v-sel-monastirische-IDPEaxm.html"),
    ("dimria", "https://dom.ria.com/uk/realty-prodaja-dom-kosachevka-rybachya-ulitsa-34741071.html"),
]

OUT = pathlib.Path('/tmp/zr_test')
OUT.mkdir(exist_ok=True)

HIDE_CSS = (
    "var css='body { zoom: 0.5 !important; } "
    "[data-testid=\"cookies-bar\"],[data-cy=\"cookies-bar\"],"
    "#onetrust-banner-sdk,.cookie-banner,[class*=\"cookie\"]"
    "{display:none !important;visibility:hidden !important;height:0 !important;}';"
    "var s=document.createElement('style');"
    "s.innerHTML=css;document.head.appendChild(s);"
)

def run(label, url, params):
    out_raw = OUT / f'{label}_raw.jpg'
    out_crop = OUT / f'{label}_cropped.jpg'
    print(f'\n=== {label} ===')
    try:
        r = httpx.get('https://api.zenrows.com/v1/', params=params, timeout=90)
        if r.status_code != 200 or not r.content.startswith((b'\x89PNG', b'\xff\xd8')):
            print(f'  FAIL: HTTP {r.status_code} {r.headers.get("content-type")} body={r.text[:150]}')
            return
        out_raw.write_bytes(r.content)
        with Image.open(out_raw) as im:
            w, h = im.size
        print(f'  raw: {w}x{h} ({len(r.content)//1024} KB)')
        src = 'olx' if 'olx' in url else 'dimria'
        crop = smart_crop_listing(out_raw, source=src)
        cw, ch = crop[2]-crop[0], crop[3]-crop[1]
        print(f'  smart_crop → ({crop[0]},{crop[1]},{crop[2]},{crop[3]}) = {cw}x{ch} aspect={cw/ch:.2f}')
        with Image.open(out_raw) as im:
            im.crop(crop).save(out_crop, quality=92)
        print(f'  saved: {out_raw} + {out_crop}')
    except Exception as e:
        print(f'  ERROR: {type(e).__name__}: {e}')

for label, url in URLS:
    run(label, url, {
        'apikey': API_KEY, 'url': url,
        'screenshot': 'true', 'screenshot_format': 'jpeg', 'screenshot_quality': 92,
        'wait': 800, 'window_width': 900, 'window_height': 1600,
        'js_render': 'true', 'device': 'desktop',
        'js_instructions': json.dumps([
            {'wait': 1200}, {'evaluate': HIDE_CSS}, {'wait': 1800}
        ]),
    })

print(f'\nResults in {OUT}/ — download via WinSCP to look at them.')
