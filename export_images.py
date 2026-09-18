#!/usr/bin/env python3
"""
export_images.py — turn cached product images into small local WebP files

Reads  : product_cache.json (313 MB dev cache, base64 blobs + image URLs),
         parts_catalog.json (representative/family mapping)
Writes : images/thumb/<slug>.webp  (~150px longest side)
         images/full/<slug>.webp   (~600px longest side)

Why this exists: embedding every cached image as base64 directly in
procurement_dashboard.html produced a 303 MB HTML file (measured, then
reverted). Separate small files are the standard static-site pattern —
cacheable, loadable in parallel, deployable to GitHub Pages without
hitting GitHub's 100 MB per-file limit (product_cache.json itself is
already over that limit and stays local-only, see .gitignore).

Family members share their representative's image (per parts_catalog.json)
so the file is written under EVERY member's own slug too — this keeps
generate_procurement.py's lookup dead simple (slug(mfr, part), no separate
family-lookup table needs to ship to the browser).

Usage:
    uv run --with Pillow --with requests python3 export_images.py
    uv run --with Pillow --with requests python3 export_images.py --limit 100
Resumable: skips any (thumb, full) pair that already exists on disk.
"""

import argparse
import base64
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
    from PIL import Image
except ImportError:
    sys.exit("Run with:  uv run --with Pillow --with requests python3 export_images.py")

ROOT        = Path(__file__).parent
CACHE_FILE  = ROOT / "product_cache.json"
CATALOG_FILE= ROOT / "parts_catalog.json"
THUMB_DIR   = ROOT / "images" / "thumb"
FULL_DIR    = ROOT / "images" / "full"
THUMB_MAX   = 150
FULL_MAX    = 600
THUMB_Q     = 72
FULL_Q      = 80
TIMEOUT     = 12
WORKERS     = 16

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://www.google.com/",
}


def slug(mfr, part):
    s = re.sub(r"[^A-Za-z0-9]+", "_", f"{mfr}_{part}").strip("_")
    return s[:120] or "unnamed"


def load_json(path):
    if not path.exists():
        sys.exit(f"Error: {path.name} not found — run from the project directory.")
    return json.loads(path.read_text(encoding="utf-8"))


def get_source_bytes(entry, session):
    """Return raw image bytes for a cache entry, decoding base64 or downloading the URL."""
    ib = entry.get("ib")
    if ib:
        try:
            b64 = ib.split(",", 1)[1] if ib.startswith("data:") else ib
            return base64.b64decode(b64)
        except Exception:
            pass
    url = entry.get("i")
    if url:
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and r.content:
                return r.content
        except Exception:
            return None
    return None


def make_webp(raw_bytes, max_dim, quality):
    from io import BytesIO
    img = Image.open(BytesIO(raw_bytes))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.mode else "RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        ratio = max_dim / max(w, h)
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, "WEBP", quality=quality, method=4)
    return buf.getvalue()


def process_one(raw_key, source_entry, member_slugs, session):
    raw_bytes = get_source_bytes(source_entry, session)
    if not raw_bytes:
        return raw_key, "no-source", 0
    try:
        thumb_bytes = make_webp(raw_bytes, THUMB_MAX, THUMB_Q)
        full_bytes = make_webp(raw_bytes, FULL_MAX, FULL_Q)
    except Exception as e:
        return raw_key, f"decode-error: {e}", 0

    written = 0
    for s in member_slugs:
        tp, fp = THUMB_DIR / f"{s}.webp", FULL_DIR / f"{s}.webp"
        if not tp.exists():
            tp.write_bytes(thumb_bytes); written += 1
        if not fp.exists():
            fp.write_bytes(full_bytes); written += 1
    return raw_key, "ok", written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="process only N representatives (testing)")
    args = ap.parse_args()

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    FULL_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading product_cache.json (this is the big one)...")
    cache = load_json(CACHE_FILE)
    catalog = load_json(CATALOG_FILE)
    lookup = catalog["lookup"]  # raw_key -> representative_key

    # Group every raw key by its representative, so we enrich/export once per rep
    # but write the file under every member's own slug too.
    reps_to_members = {}
    for raw_key, rep_key in lookup.items():
        reps_to_members.setdefault(rep_key, []).append(raw_key)

    # Skip representatives whose (thumb, full) already exist for ALL their members —
    # resumability. Cheap existence check per member.
    work = []
    for rep_key, member_keys in reps_to_members.items():
        member_slugs = []
        for mk in member_keys:
            mfr, part = mk.split("||", 1)
            s = slug(mfr, part)
            if not (THUMB_DIR / f"{s}.webp").exists() or not (FULL_DIR / f"{s}.webp").exists():
                member_slugs.append(s)
        if member_slugs:
            work.append((rep_key, member_slugs))

    if args.limit:
        work = work[: args.limit]

    print(f"Representatives to process: {len(work)} (of {len(reps_to_members)} total)")

    session = requests.Session()
    ok = skipped_no_source = errors = files_written = 0
    t0 = time.time()

    def submit(rep_key, member_slugs):
        entry = cache.get(rep_key) or {}
        if not entry or not (entry.get("ib") or entry.get("i")):
            # representative has no image data — fall back to any member's own cache entry
            for mk in reps_to_members.get(rep_key, []):
                e = cache.get(mk) or {}
                if e.get("ib") or e.get("i"):
                    entry = e
                    break
        return process_one(rep_key, entry, member_slugs, session)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(submit, rep_key, member_slugs): rep_key for rep_key, member_slugs in work}
        for i, fut in enumerate(as_completed(futures), 1):
            raw_key, status, written = fut.result()
            if status == "ok":
                ok += 1
                files_written += written
            elif status == "no-source":
                skipped_no_source += 1
            else:
                errors += 1
            if i % 250 == 0 or i == len(work):
                elapsed = time.time() - t0
                print(f"  [{i}/{len(work)}] ok={ok} no-source={skipped_no_source} "
                      f"errors={errors} files={files_written} ({elapsed:.0f}s)")

    print(f"\nDone in {time.time()-t0:.0f}s. ok={ok} no-source={skipped_no_source} "
          f"errors={errors} files_written={files_written}")


if __name__ == "__main__":
    main()
