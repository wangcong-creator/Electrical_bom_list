#!/usr/bin/env python3
"""
enrich_products.py — Product information enricher for BBAC procurement data

Fetches product title, description and image URL for each unique part from
distributor / manufacturer websites, caches results in product_cache.json.

Enrichment order per part:
  1. Mouser Search API   (if MOUSER_API_KEY is set)       — exact MPN lookup, verified
  2. Nexar/Octopart API  (if NEXAR_CLIENT_ID/SECRET set)  — exact MPN lookup, verified
  3. Per-manufacturer scraper / generic RS+automation24+Mouser-page search — verified
  4. Bing Images last resort — verified

"Verified" means verify_match() requires a normalized fragment of the part number
AND (when we have a manufacturer code to check) the manufacturer name to both appear
in the candidate's title/description/source-url before it's accepted. A candidate
that fails is rejected — the part is left without an image rather than getting a
wrong one. This directly targets the ~340 wrong-brand cache entries found in an
audit of the previous, unverified scraper.

If parts_catalog.json (see build_parts_catalog.py) is present, only "representative"
parts are enriched — family members (e.g. the same cable at different lengths) get
the representative's result copied onto them afterwards, tagged "inherited_from".
Without parts_catalog.json every unique (manufacturer, part) pair is enriched
individually, same as before.

Usage:
    uv run --with requests --with beautifulsoup4 --with pandas --with Pillow python enrich_products.py
    uv run --with requests --with beautifulsoup4 --with pandas --with Pillow python enrich_products.py --limit 200
    uv run --with requests --with beautifulsoup4 --with pandas --with Pillow python enrich_products.py --mfr SIEMENS
    uv run --with requests --with beautifulsoup4 --with pandas --with Pillow python enrich_products.py --retry-empty
    uv run --with requests --with beautifulsoup4 --with pandas --with Pillow python enrich_products.py --reset

    MOUSER_API_KEY=...                              enables the Mouser API tier
    NEXAR_CLIENT_ID=... NEXAR_CLIENT_SECRET=...      enables the Nexar/Octopart API tier

Interrupt with Ctrl+C any time — progress is saved after every part.
Run again to continue from where you left off.
"""

import sys, json, time, re, argparse, base64, os
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
    import pandas as pd
except ImportError:
    sys.exit("Run with:  uv run --with requests --with beautifulsoup4 --with pandas --with Pillow python enrich_products.py")

try:
    from PIL import Image
    from io import BytesIO
except ImportError:
    Image = None  # large-image resizing is skipped (old skip-if->200KB behavior) if Pillow isn't installed

# ── Config ─────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
CACHE_FILE   = SCRIPT_DIR / "product_cache.json"
CATALOG_FILE = SCRIPT_DIR / "parts_catalog.json"
CSV_FILE     = str(SCRIPT_DIR / "KW07_Material status-20230220_with price.csv")
DELAY_SHORT  = 1.5   # between requests to the same domain
DELAY_LONG   = 3.0   # after a non-200 / rate-limit
TIMEOUT      = 14

MOUSER_API_KEY      = os.environ.get("MOUSER_API_KEY", "")
NEXAR_CLIENT_ID     = os.environ.get("NEXAR_CLIENT_ID", "")
NEXAR_CLIENT_SECRET = os.environ.get("NEXAR_CLIENT_SECRET", "")
_nexar_token = {"value": "", "expires": 0}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ── Cache ──────────────────────────────────────────────────────────────────
def load_cache():
    if CACHE_FILE.exists():
        try:   return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except: return {}
    return {}

def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

def load_catalog():
    if CATALOG_FILE.exists():
        try:   return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        except: return None
    return None

def ckey(mfr, part):
    return f"{mfr}||{part}"

# ── Verification ──────────────────────────────────────────────────────────
import unicodedata

_GERMAN_ASCII_FOLD = str.maketrans({
    "Ü": "UE", "ü": "ue", "Ä": "AE", "ä": "ae", "Ö": "OE", "ö": "oe", "ß": "ss",
})

def norm_text(s):
    """Uppercase, strip punctuation/whitespace, and ASCII-fold accented characters —
    German umlauts get the conventional 'ue'/'ae'/'oe'/'ss' expansion (not just the
    bare vowel an NFKD strip alone would leave), other diacritics (é, ñ, ...) via NFKD.
    Found live: without this, "Weidmüller" (as scraped, with umlaut) and "WEIDMUELLER"
    (the CSV's manufacturer code) normalized to different strings, so verify_match()
    would incorrectly REJECT genuine Weidmüller/Lütze matches — a false negative that
    silently narrows API/scrape acceptance for every German-umlaut manufacturer name
    in this BOM."""
    s = s.translate(_GERMAN_ASCII_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()

def verify_match(mfr, part, info):
    """Require a normalized fragment of the part number to appear in the candidate's
    ACTUAL PAGE CONTENT (title + description only — never the source URL, which for a
    search-URL we constructed from the part number would trivially always "match",
    defeating the check; confirmed live: a Murrelektronik homepage at .../?q=55529
    passed a URL-inclusive check purely because the query string echoed the part
    number back, even though the page was generic "about us" boilerplate with a
    marketing photo, not the product). The manufacturer name (if we have a code to
    check) may still count the URL, since a matching domain (e.g. murrelektronik.com)
    is a genuine signal, not an artifact of how we built the request."""
    if not info:
        return False
    content_norm = norm_text(f"{info.get('t','')} {info.get('d','')}")
    part_norm = norm_text(part)
    if not part_norm:
        return False
    needle = part_norm[:12]  # long modular/configurator part numbers only need a solid prefix
    if len(needle) < 3 or needle not in content_norm:
        return False
    mfr_norm = norm_text(mfr)
    if not mfr_norm:
        return True
    text_norm = content_norm + norm_text(info.get("u",""))
    mfr_needle = mfr_norm[:5] if len(mfr_norm) >= 5 else mfr_norm
    return mfr_needle in text_norm

# ── Network ────────────────────────────────────────────────────────────────
def fetch(url):
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser"), r.url
        if r.status_code in (429, 503):
            time.sleep(DELAY_LONG)
    except Exception:
        pass
    return None, url

def abs_url(src, base):
    if not src: return ""
    if src.startswith("//"): return "https:" + src
    if src.startswith("http"): return src
    return urljoin(base, src)

# RS CDN images hotlink fine — no need to embed them as base64
_CDN_OK = ("media.rs-online.com", "images.rs-online.com")

# Small, explicitly hand-verified allowlist of image CDNs known to serve real product
# photos for hosts that don't share a domain with the page they were found on.
_TRUSTED_IMAGE_HOSTS = {
    "media.rs-online.com", "images.rs-online.com",
    "mm.digikey.com",  # DigiKey's real product-image CDN — confirmed live during a
                        # retroactive cache audit; a legitimate global distributor,
                        # just not the same domain as whatever page it was found on.
}

def _registrable_domain(host):
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()

def image_domain_trusted(img_url, page_url):
    """An extracted image is only trusted if it's hosted on (a) the same site as the
    page it was scraped from, or (b) a small explicit CDN allowlist. Rejects anything
    else outright rather than trying to maintain an ever-growing blocklist of bad
    sources. This exists because og:image / "largest <img> on page" can pick up an ad,
    widget, or embedded-video thumbnail instead of the actual product photo — confirmed
    live on real destaco.com product pages (the correct page, correct title/part number)
    whose extracted image turned out to be a YouTube video thumbnail, a Freepik stock
    vector of "scared people", and a video-game cover art from an unrelated Philippine
    gaming/collectibles store. All three shared nothing with destaco.com's own domain —
    this check would have rejected every one of them instead of accepting a random image
    from wherever the page happened to embed one."""
    img_host = urlparse(img_url).netloc.lower()
    if not img_host:
        return False
    if img_host in _TRUSTED_IMAGE_HOSTS:
        return True
    page_host = urlparse(page_url or "").netloc.lower()
    if page_host and _registrable_domain(img_host) == _registrable_domain(page_host):
        return True
    return False

def upsize_rs_image(url):
    """RS Components' CDN serves listing-thumbnail presets (t_plp ~168x144,
    t_thumb100) when a result came from a search-results page rather than a
    product-detail page. Strip the preset segment entirely to get the original
    full-size image (verified live: media.rs-online.com/Y2542969-01.jpg without
    any /t_xxx/ segment returns a real 1000x857 photo, vs 168x144 for /t_plp/ —
    a made-up preset name like "t_extralarge" that isn't a real RS preset would
    silently 400 instead, which is what an earlier version of this function did
    before being caught and fixed)."""
    if not url:
        return url
    return re.sub(r"/t_(plp|thumb100)/", "/", url)

def fetch_image_b64(url, size_limit=200_000, max_dim=800):
    """Download an image and return a base64 data URL. Images over size_limit are
    resized down to max_dim (longest side) via Pillow instead of being discarded —
    this used to just return None for anything >200KB, silently biasing every
    embedded image toward smaller/lower-resolution sources."""
    if not url: return None
    host = urlparse(url).netloc
    if any(cdn in host for cdn in _CDN_OK):
        return None  # CDN images work fine via URL, skip download
    try:
        r = SESSION.get(url, timeout=10, stream=True)
        if r.status_code != 200: return None
        ct = r.headers.get("content-type", "")
        if not ct.startswith("image/"): return None
        chunks, total = [], 0
        for chunk in r.iter_content(8192):
            total += len(chunk)
            chunks.append(chunk)
            if total > 8_000_000:  # sanity cap so a mislabeled huge file can't exhaust memory
                return None
        raw = b"".join(chunks)
        mime = ct.split(";")[0].strip() or "image/jpeg"

        if total > size_limit and Image is not None:
            try:
                img = Image.open(BytesIO(raw))
                img.load()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA" if "A" in img.mode else "RGB")
                w, h = img.size
                if max(w, h) > max_dim:
                    ratio = max_dim / max(w, h)
                    img = img.resize((max(1, int(w*ratio)), max(1, int(h*ratio))), Image.LANCZOS)
                buf = BytesIO()
                img.save(buf, "JPEG", quality=85)
                raw, mime = buf.getvalue(), "image/jpeg"
            except Exception:
                return None
        elif total > size_limit:
            return None  # Pillow unavailable — fall back to the old behavior

        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None

# ── Distributor APIs (verified structured data — tried before any scraping) ─
_api_disabled = {"mouser": False, "nexar": False}  # set True after a quota/auth error so we
                                                     # stop burning requests and time on every
                                                     # remaining part for the rest of this run

def _disable_api(name, reason):
    if not _api_disabled[name]:
        _api_disabled[name] = True
        print(f"\n*** {name.upper()} API disabled for the rest of this run: {reason} ***\n")

def mouser_lookup(mfr, part):
    """Mouser Search API — exact MPN lookup. Free key from mouser.com/api-search.
    Returns a verified {t,d,i,u,source} dict, or None. A quota/auth error is NOT the same
    as "part not found" — it disables the API for the rest of the run instead of silently
    returning None on every subsequent call (which previously masqueraded as a 0% hit rate
    when the real cause was an exhausted/pending API key)."""
    if not MOUSER_API_KEY or _api_disabled["mouser"]:
        return None
    try:
        r = SESSION.post(
            f"https://api.mouser.com/api/v1/search/partnumber?apiKey={MOUSER_API_KEY}",
            json={"SearchByPartRequest": {"mouserPartNumber": part}},
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code == 401 or r.status_code == 403:
            _disable_api("mouser", f"HTTP {r.status_code}")
            return None
        if r.status_code != 200:
            return None
        body = r.json()
        errors = body.get("Errors") or []
        if errors:
            msg = errors[0].get("Message", "")
            code = errors[0].get("Code", "")
            # "Invalid unique identifier" on the API Key field = bad/pending/unauthorized key —
            # every subsequent call this run will fail the exact same way.
            if code == "Invalid" and errors[0].get("PropertyName") == "API Key":
                _disable_api("mouser", f"invalid or not-yet-authorized API key ({msg})")
            return None
        parts = ((body.get("SearchResults") or {}).get("Parts")) or []
        for p in parts:
            mpn_norm = re.sub(r"[^A-Z0-9]", "", (p.get("ManufacturerPartNumber") or "").upper())
            if mpn_norm != re.sub(r"[^A-Z0-9]", "", part.upper()):
                continue
            img = p.get("ImagePath") or ""
            if not img:
                continue
            candidate = {
                "t": f"{p.get('Manufacturer','')} — {p.get('Description','') or part}".strip(" —"),
                "d": p.get("Description", ""),
                "i": img,
                "u": p.get("ProductDetailUrl", ""),
            }
            if verify_match(mfr, part, candidate):
                candidate["source"] = "mouser"
                return candidate
    except Exception:
        pass
    return None

def _nexar_token_valid():
    return _nexar_token["value"] and time.time() < _nexar_token["expires"] - 30

def nexar_auth():
    if _nexar_token_valid():
        return _nexar_token["value"]
    if not (NEXAR_CLIENT_ID and NEXAR_CLIENT_SECRET):
        return ""
    try:
        r = SESSION.post(
            "https://identity.nexar.com/connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": NEXAR_CLIENT_ID,
                "client_secret": NEXAR_CLIENT_SECRET,
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        _nexar_token["value"] = data.get("access_token", "")
        _nexar_token["expires"] = time.time() + int(data.get("expires_in", 3600))
        return _nexar_token["value"]
    except Exception:
        return ""

def nexar_lookup(mfr, part):
    """Nexar/Octopart GraphQL API — exact MPN lookup aggregated across many
    distributors. Free OAuth2 app credentials from nexar.com.
    Returns a verified {t,d,i,u,source} dict, or None. Like mouser_lookup(), a quota/plan
    error disables the API for the rest of the run rather than being treated as "not found"
    on every remaining part — confirmed live: a free-tier app here had a 10-lookup total
    quota, and every call after that returned a GraphQL error ("exceeded your part limit
    of 10") that the original code silently swallowed as None, making a fully-exhausted
    quota indistinguishable from a genuine 0% catalog coverage result."""
    if _api_disabled["nexar"]:
        return None
    token = nexar_auth()
    if not token:
        return None
    # NOTE: the SupPart type has no "imageUrl" field — confirmed via live schema
    # introspection (__type(name:"SupPart")) after the API rejected that name.
    # The actual field is bestImage{url}.
    query = (
        'query($q:String!){ supSearchMpn(q:$q, limit:3) { results { part { '
        'mpn manufacturer{name} shortDescription bestImage{url} bestDatasheet{url} } } } }'
    )
    try:
        r = SESSION.post(
            "https://api.nexar.com/graphql",
            json={"query": query, "variables": {"q": part}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        body = r.json()
        errors = body.get("errors") or []
        if errors:
            msg = errors[0].get("message", "")
            if "part limit" in msg.lower() or "upgrade your plan" in msg.lower():
                _disable_api("nexar", msg)
            return None
        results = ((body.get("data") or {}).get("supSearchMpn") or {}).get("results") or []
        for res in results:
            p = res.get("part") or {}
            mpn_norm = re.sub(r"[^A-Z0-9]", "", (p.get("mpn") or "").upper())
            if mpn_norm != re.sub(r"[^A-Z0-9]", "", part.upper()):
                continue
            img = (p.get("bestImage") or {}).get("url") or ""
            if not img:
                continue
            mfr_name = (p.get("manufacturer") or {}).get("name", "")
            candidate = {
                "t": f"{mfr_name} — {p.get('shortDescription','') or part}".strip(" —"),
                "d": p.get("shortDescription", ""),
                "i": img,
                "u": (p.get("bestDatasheet") or {}).get("url", "") or "",
            }
            if verify_match(mfr, part, candidate):
                candidate["source"] = "nexar"
                return candidate
    except Exception:
        pass
    return None

# ── Info extraction ────────────────────────────────────────────────────────
def extract_info(soup, final_url):
    if not soup: return None

    def meta(prop, attr="property"):
        t = soup.find("meta", {attr: prop})
        return (t.get("content") or "").strip() if t else ""

    title = meta("og:title") or meta("title", "name")
    desc  = meta("og:description") or meta("description", "name")
    img   = meta("og:image")

    # JSON-LD (often richer — has image, name, description)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(tag.string or "")
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                t = item.get("@type","")
                if t in ("Product","ItemPage","WebPage"):
                    title = title or item.get("name","")
                    desc  = desc  or item.get("description","")
                    imgs  = item.get("image","")
                    if isinstance(imgs, list) and imgs: img = img or imgs[0]
                    elif isinstance(imgs, str):          img = img or imgs
                    elif isinstance(imgs, dict):         img = img or imgs.get("url","")
        except Exception: pass

    # Fallback: find the largest meaningful <img>
    if not img:
        candidates = []
        for itag in soup.find_all("img"):
            src = (itag.get("src") or itag.get("data-src") or "").strip()
            if not src: continue
            low = src.lower()
            if any(x in low for x in ("logo","icon","sprite","pixel","1x1","blank",".svg","banner","flag")):
                continue
            w = int(re.sub(r"\D","",itag.get("width","0") or "0") or 0)
            h = int(re.sub(r"\D","",itag.get("height","0") or "0") or 0)
            # prefer larger images, skip very small
            size = w * h if w and h else 10000
            if (w == 0 or w >= 60) and (h == 0 or h >= 60) and size >= 3600:
                candidates.append((size, src))
        if candidates:
            candidates.sort(reverse=True)
            img = abs_url(candidates[0][1], final_url)

    if not title and soup.title:
        title = soup.title.text.strip()

    title = title[:200].strip()
    desc  = re.sub(r"\s+", " ", desc)[:500].strip()
    if img and img.startswith("//"):
        img = "https:" + img
    img = upsize_rs_image(img)
    # RS Components serves a generic "no photo available" placeholder graphic
    # (media.rs-online.com/noImage.png) instead of a 404 when a product has no real
    # photo. Confirmed live: 992 cache entries had this treated as a real image
    # because it's a normal 200-OK JPEG-shaped URL — filter it out so a placeholder
    # graphic is never mistaken for a product photo.
    if img and re.search(r"/noimage\.(png|jpg|jpeg|gif)$", img, re.IGNORECASE):
        img = ""
    if img and not image_domain_trusted(img, final_url):
        img = ""

    skip_titles = {"search results","search","home","products","404","not found"}
    if title.lower() in skip_titles: title = ""

    if not title and not img: return None
    return {"t": title, "d": desc, "i": img, "u": str(final_url)}

# ── Source strategies ──────────────────────────────────────────────────────
def try_urls(url_list, mfr, part):
    """Fetch each candidate URL in order; the first one whose extracted info passes
    verify_match() wins. A page whose title/description names a different part or
    manufacturer is rejected instead of accepted (this is the fix for the ~340
    wrong-brand cache entries a prior audit found)."""
    for url in url_list:
        soup, final = fetch(url)
        info = extract_info(soup, final)
        if info and (info.get("i") or info.get("d")) and verify_match(mfr, part, info):
            info["source"] = "scrape"
            return info
        time.sleep(DELAY_SHORT)
    return None

def siemens(mfr, part):
    clean = part.replace(" ","").replace("_","")
    return try_urls([
        f"https://mall.industry.siemens.com/mall/en/WW/Catalog/Product/{quote(clean)}",
        f"https://support.industry.siemens.com/cs/ww/en/ps/{quote(clean)}",
        f"https://www.automation24.com/search?q={quote(part)}",
    ], mfr, part)

def festo(mfr, part):
    clean = re.sub(r"[_\s].*","",part)   # FESTO sometimes has suffixes
    return try_urls([
        f"https://www.festo.com/cat/en-gb_gb/search?query={quote(clean)}",
        f"https://www.automation24.com/search?q=festo+{quote(part)}",
        f"https://uk.rs-online.com/web/c/?searchTerm={quote(mfr+' '+part)}&redirect=y",
    ], mfr, part)

def pepperl_fuchs(mfr, part):
    return try_urls([
        f"https://www.pepperl-fuchs.com/global/en/search.html?q={quote(part)}",
        f"https://www.automation24.com/search?q=pepperl+fuchs+{quote(part)}",
        f"https://uk.rs-online.com/web/c/?searchTerm={quote(mfr+' '+part)}&redirect=y",
    ], mfr, part)

def murr(mfr, part):
    return try_urls([
        f"https://www.murrelektronik.com/en/search/?q={quote(part)}",
        f"https://www.automation24.com/search?q=murr+{quote(part)}",
        f"https://uk.rs-online.com/web/c/?searchTerm={quote(mfr+' '+part)}&redirect=y",
    ], mfr, part)

def ifm(mfr, part):
    return try_urls([
        f"https://www.ifm.com/us/en/search.html?q={quote(part)}",
        f"https://www.automation24.com/search?q=ifm+{quote(part)}",
    ], mfr, part)

def destaco(mfr, part):
    slug = part.lower().replace(" ", "-")
    return try_urls([
        f"https://www.destaco.com/{slug}",
        f"https://www.automation24.com/search?q=destaco+{quote(part)}",
        f"https://uk.rs-online.com/web/c/?searchTerm={quote(mfr+' '+part)}&redirect=y",
    ], mfr, part)

def smc(mfr, part):
    return try_urls([
        f"https://www.smcworld.com/en-JP/search.php?q={quote(part)}",
        f"https://www.automation24.com/search?q=smc+{quote(part)}",
        f"https://uk.rs-online.com/web/c/?searchTerm={quote(mfr+' '+part)}&redirect=y",
    ], mfr, part)

def wago(mfr, part):
    return try_urls([
        f"https://www.wago.com/global/search#/?q={quote(part)}",
        f"https://uk.rs-online.com/web/c/?searchTerm={quote(mfr+' '+part)}&redirect=y",
    ], mfr, part)

def phoenix_contact(mfr, part):
    return try_urls([
        f"https://www.phoenixcontact.com/en-global/products/search?query={quote(part)}",
        f"https://uk.rs-online.com/web/c/?searchTerm={quote(mfr+' '+part)}&redirect=y",
    ], mfr, part)

def generic(mfr, part):
    """Fallback for any manufacturer not in MFR_HANDLERS: RS Components + automation24 +
    Mouser's own search page, now queried with manufacturer+part (not part alone) so
    RS's search is more likely to land on the right product, then verified regardless."""
    q = f"{mfr} {part}"
    return try_urls([
        f"https://uk.rs-online.com/web/c/?searchTerm={quote(q)}&redirect=y",
        f"https://www.automation24.com/search?q={quote(q)}",
        f"https://www.mouser.com/Search/Refine?Keyword={quote(part)}",
    ], mfr, part)

MFR_HANDLERS = {
    "SIEMENS":          siemens,
    "FESTO":            festo,
    "PEPPERL&FUCHS":    pepperl_fuchs,
    "MURRELEKTRONIK":   murr,
    "MURR":             murr,
    "IFM":              ifm,
    "DESTACO":          destaco,
    "SMC":              smc,
    "WAGO":             wago,
    "PHOENIX CONTACT":  phoenix_contact,
}

def bing_image(mfr, part):
    """Scrape Bing Images as a last resort — now checks EACH candidate result against
    verify_match() (using the image's associated page title/url) and returns the first
    one that passes, instead of blindly taking whatever result comes first."""
    q = f"{mfr} {part}"
    url = f"https://www.bing.com/images/search?q={quote(q)}&first=1&count=8"
    soup, _ = fetch(url)
    if not soup: return None
    # Bing wraps each result in <a class="iusc" m='{"murl":"...","purl":"...", ...}'>
    for a in soup.find_all("a", class_="iusc"):
        try:
            data = json.loads(a.get("m", "{}"))
            img = data.get("murl", "")
            if not img.startswith("http"):
                continue
            page_url = data.get("purl", "")
            # associated caption text sits in a sibling element in Bing's markup
            caption = ""
            parent = a.find_parent("div", class_="imgpt") or a.parent
            if parent:
                cap_el = parent.find_next("div", class_="infnmpt")
                if cap_el: caption = cap_el.get_text(" ", strip=True)
            candidate = {"t": caption, "d": "", "i": img, "u": page_url}
            if verify_match(mfr, part, candidate):
                candidate["source"] = "bing"
                return candidate
        except Exception:
            continue
    return None

def enrich_one(mfr, part):
    result = mouser_lookup(mfr, part)
    if not result:
        result = nexar_lookup(mfr, part)
    if not result:
        handler = MFR_HANDLERS.get(mfr.upper())
        result = handler(mfr, part) if handler else None
    if not result:
        result = generic(mfr, part)
    if not (result and result.get("i")):
        bing = bing_image(mfr, part)
        if bing:
            result = result or {"t": "", "d": "", "u": "", "source": "bing"}
            result["i"] = bing["i"]
            result.setdefault("source", "bing")
    return result

# ── Main ───────────────────────────────────────────────────────────────────
def safe_print(text):
    """Print with non-ASCII chars replaced so Windows console doesn't crash."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit",       type=int, default=0,  help="Stop after N new parts (0=all)")
    p.add_argument("--reset",       action="store_true",  help="Clear cache first")
    p.add_argument("--mfr",         default="",           help="Only this manufacturer code")
    p.add_argument("--fix-images",  action="store_true",  help="Re-fetch images for cached entries that have none")
    p.add_argument("--retry-empty", action="store_true",  help="Re-run only cache entries that are currently empty ({})")
    args = p.parse_args()

    safe_print("Loading CSV...")
    df = pd.read_csv(CSV_FILE, encoding="utf-8-sig", on_bad_lines="skip")
    df["Manufact."]  = df["Manufact."].fillna("").astype(str).str.strip()
    df["Manufacturer Part Number"] = df["Manufacturer Part Number"].fillna("").astype(str).str.strip()

    def parse_val(v):
        try:   return float(str(v).replace(",",""))
        except: return 0.0
    df["value"] = df["Net Order Value"].apply(parse_val)

    parts_df = (
        df[df["Manufacturer Part Number"] != ""]
        .groupby(["Manufact.","Manufacturer Part Number"], as_index=False)["value"]
        .sum()
        .sort_values("value", ascending=False)
        .reset_index(drop=True)
    )
    if args.mfr:
        parts_df = parts_df[parts_df["Manufact."].str.upper() == args.mfr.upper()]

    # ── Parts catalog: enrich representatives only, family members inherit ──
    catalog = load_catalog()
    if catalog:
        rep_of = catalog["lookup"]  # raw_key -> representative_key
        parts_df["_key"] = parts_df["Manufact."] + "||" + parts_df["Manufacturer Part Number"]
        parts_df["_is_rep"] = parts_df["_key"].map(lambda k: rep_of.get(k, k) == k)
        n_all = len(parts_df)
        parts_df = parts_df[parts_df["_is_rep"]]
        safe_print(f"Parts catalog loaded : {n_all:,} parts -> {len(parts_df):,} representatives to enrich "
                    f"({catalog.get('families',0)} families cover {catalog.get('family_members',0)} members)")
    else:
        safe_print("No parts_catalog.json found — enriching every unique part individually "
                    "(run build_parts_catalog.py first to enrich only representatives and save work).")

    safe_print(f"Unique parts : {len(parts_df):,}  (sorted by value, highest first)")

    if args.reset and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        safe_print("Cache cleared.")

    cache = load_cache()
    safe_print(f"Cache loaded : {len(cache):,} entries already cached")
    if MOUSER_API_KEY:  safe_print("Mouser API   : enabled")
    if NEXAR_CLIENT_ID: safe_print("Nexar API    : enabled")

    # ── --fix-images mode: patch cached entries that have no image ────────────
    if args.fix_images:
        need_b64 = [(k, v) for k, v in cache.items()
                    if v and v.get("i") and not v.get("ib")
                    and not any(cdn in urlparse(v["i"]).netloc for cdn in _CDN_OK)]
        safe_print(f"Fix-images   : {len(need_b64)} non-CDN entries need base64 embedding")
        fixed = 0
        for idx, (key, entry) in enumerate(need_b64):
            mfr, part = key.split("||", 1)
            ib = fetch_image_b64(entry["i"])
            if not ib:
                bing = bing_image(mfr, part)
                if bing and bing.get("i"):
                    ib = fetch_image_b64(bing["i"])
                    if ib:
                        entry["i"] = bing["i"]
            if ib:
                entry["ib"] = ib
                cache[key] = entry
                save_cache(cache)
                fixed += 1
                safe_print(f"  [{idx+1:4d}/{len(need_b64)}] OK  {mfr[:15]:<15} | {part[:38]}")
            else:
                safe_print(f"  [{idx+1:4d}/{len(need_b64)}] --  {mfr[:15]:<15} | {part[:38]}")
            time.sleep(DELAY_SHORT)
            if args.limit and idx + 1 >= args.limit:
                break
        safe_print(f"\nEmbedded {fixed} images as base64. Run export_images.py then generate_procurement.py to rebuild.")
        return

    # ── --retry-empty mode: re-run only currently-empty cache entries ────────
    if args.retry_empty:
        empty_keys = {k for k, v in cache.items() if not v}
        parts_df = parts_df[(parts_df["Manufact."] + "||" + parts_df["Manufacturer Part Number"]).isin(empty_keys)]
        safe_print(f"Retry-empty  : {len(parts_df):,} representatives currently have no data")
        # Only clear cache entries for rows we'll actually attempt THIS run (respects --limit) —
        # clearing the whole empty set up front would silently drop the rest to "absent from
        # cache" instead of "{}" without reprocessing them in this run.
        to_clear = parts_df["Manufact."] + "||" + parts_df["Manufacturer Part Number"]
        if args.limit:
            to_clear = to_clear.head(args.limit)
        for k in to_clear:
            cache.pop(k, None)

    processed = found = 0
    t0 = time.time()

    for _, row in parts_df.iterrows():
        mfr  = row["Manufact."]
        part = row["Manufacturer Part Number"]
        val  = row["value"]
        key  = ckey(mfr, part)

        if key in cache:
            continue

        try:
            info = enrich_one(mfr, part)
        except KeyboardInterrupt:
            safe_print(f"\nInterrupted. {processed} new entries saved.")
            sys.exit(0)
        except Exception as e:
            info = None
            safe_print(f"  ERROR: {e}")

        # Download small non-CDN images as base64 to avoid hotlink failures
        if info and info.get("i"):
            ib = fetch_image_b64(info["i"])
            if ib:
                info["ib"] = ib

        cache[key] = info or {}
        save_cache(cache)
        processed += 1

        if info and (info.get("i") or info.get("d")):
            found += 1
            status = f"OK  img={'Y' if info.get('i') else 'N'}  src={info.get('source','?'):<7}  {info.get('t','')[:45]}"
        else:
            status = "--- not found"

        elapsed = time.time() - t0
        rate    = processed / elapsed if elapsed else 1
        remaining = (len(parts_df) - processed) / rate
        eta = f"{remaining/60:.0f}min" if remaining > 60 else f"{remaining:.0f}s"

        safe_print(f"[{processed:5d}/{len(parts_df):5d}] {mfr[:15]:<15} | "
                   f"{part[:38]:<38} | {val:>10,.0f} | {status}  ETA:{eta}")

        if args.limit and processed >= args.limit:
            safe_print(f"\nReached --limit {args.limit}. Run again to continue.")
            break

    # ── Propagate representative results onto family members ────────────────
    if catalog:
        propagated = 0
        for rep_key, members in catalog.get("families_detail", {}).items():
            rep_entry = cache.get(rep_key)
            if not rep_entry or not (rep_entry.get("t") or rep_entry.get("i") or rep_entry.get("d")):
                continue  # representative has no data yet — don't clobber any existing member data
            for member_key in members:
                inherited = dict(rep_entry)
                inherited["inherited_from"] = rep_key
                cache[member_key] = inherited
                propagated += 1
        if propagated:
            save_cache(cache)
            safe_print(f"Propagated {propagated} representative results onto family members.")

    elapsed = time.time() - t0
    total = len(cache)
    safe_print(f"\nFinished: {processed} new  |  {found} with data ({found*100//max(processed,1)}%)"
               f"  |  {total} total in cache  |  {elapsed/60:.1f} min")
    for name in ("mouser", "nexar"):
        if _api_disabled[name]:
            safe_print(f"NOTE: {name} API was disabled partway through this run (quota/auth "
                       f"error) — the hit rate above under-represents what it could find with "
                       f"a working key/quota. Re-run once fixed; already-cached entries are skipped.")
    safe_print("Next: python export_images.py  then  python generate_procurement.py")

if __name__ == "__main__":
    main()
