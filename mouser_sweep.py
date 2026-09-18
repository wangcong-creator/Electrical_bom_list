#!/usr/bin/env python3
"""Mouser-only sweep over every currently-empty catalog representative.

Unlike `enrich_products.py --retry-empty`, this skips the slow per-manufacturer
scraper / Bing fallback chain entirely -- it only calls the Mouser Search API
(fast, ~1 req/sec) via enrich_products.mouser_lookup(), which already applies
verify_match() (part number + manufacturer must both match) before accepting
a hit. A miss is left alone (not written as {}), so a normal enrich_products.py
run can still attempt it later via the scraper/Bing tiers.

Usage: uv run --with requests --with beautifulsoup4 --with pandas --with Pillow python3 mouser_sweep.py
"""
import json, time, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import enrich_products as ep

def main():
    if not ep.MOUSER_API_KEY:
        sys.exit("MOUSER_API_KEY not set")

    cache = ep.load_cache()
    catalog = ep.load_catalog()
    lookup = catalog["lookup"]
    reps = sorted(set(lookup.values()))

    empty = [r for r in reps if not (cache.get(r) and (cache[r].get("i") or cache[r].get("ib")))]
    print(f"Representatives without an image: {len(empty)}")

    hits = 0
    errors = 0
    rate_limit_backoffs = 0
    t0 = time.time()
    i = 0
    while i < len(empty):
        key = empty[i]
        mfr, part = key.split("||", 1)
        try:
            info = ep.mouser_lookup(mfr, part)
        except Exception:
            info = None
            errors += 1

        if ep._api_disabled["mouser"]:
            # mouser_lookup() calls _disable_api() on ANY error response, including a
            # transient per-minute rate limit -- distinguish that from a real quota/auth
            # failure by checking the raw error code before giving up for good.
            try:
                r = ep.SESSION.post(
                    f"https://api.mouser.com/api/v1/search/partnumber?apiKey={ep.MOUSER_API_KEY}",
                    json={"SearchByPartRequest": {"mouserPartNumber": "0"}},
                    headers={"Content-Type": "application/json"}, timeout=14,
                )
                code = ((r.json().get("Errors") or [{}])[0]).get("Code", "")
            except Exception:
                code = ""
            if code == "TooManyRequests":
                rate_limit_backoffs += 1
                print(f"[{i+1:5d}/{len(empty)}] rate-limited, backing off 65s (backoff #{rate_limit_backoffs})")
                ep._api_disabled["mouser"] = False
                time.sleep(65)
                continue  # retry same item, don't advance i
            else:
                print(f"\nMouser API disabled at item {i+1}/{len(empty)} (non-rate-limit error, code={code!r}). Stopping.")
                break

        if info and info.get("i"):
            ib = ep.fetch_image_b64(info["i"])
            if ib:
                info["ib"] = ib
            cache[key] = info
            ep.save_cache(cache)
            hits += 1
            print(f"[{i+1:5d}/{len(empty)}] HIT  {mfr[:20]:<20} | {part[:30]:<30} | {info.get('t','')[:40]}")
        elif (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (len(empty) - i - 1) / rate
            print(f"[{i+1:5d}/{len(empty)}] ... {hits} hits so far  ETA:{eta/60:.0f}min")

        i += 1
        time.sleep(2.2)  # stay under Mouser's free-tier per-minute call limit

    elapsed = time.time() - t0
    print(f"\nDone: {hits} hits / {len(empty)} checked in {elapsed/60:.1f}min ({errors} request errors)")

if __name__ == "__main__":
    main()
