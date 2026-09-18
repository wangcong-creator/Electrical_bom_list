#!/usr/bin/env python3
"""
build_parts_catalog.py — canonical parts catalog for the KW07 procurement pipeline

Reads  : KW07_Material status-20230220_with price.csv
Writes : parts_catalog.json

Every "manufacturer||part number" pair in the CSV maps to a representative
key in `lookup`. Most parts are their own representative (singletons).
Two kinds of parts are folded onto a shared representative instead:

  1. Pure formatting duplicates — the same physical part written with
     different trailing punctuation (FESTO's modular part-number
     configurator strings are the main source: "...NGKAQFS", "...NGKAQFS_",
     "...NGKAQFS-", "...NGKAQFS." all name one catalog item).
  2. Part families — parts that share a manufacturer and a >=5-character
     prefix once a trailing numeric/length/serial suffix is stripped, e.g.
     F.EE "EGT001L=0088" / "EGT001L=0148" / ... (cable harnesses cut to
     different lengths). These are visually the same item, so one
     enriched image can be reused across every member.

`enrich_products.py` should enrich only keys where lookup[key] == key
(representatives), then copy the resulting cache record onto every other
key that maps to that representative.
"""

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CSV_FILE = Path(__file__).parent / "KW07_Material status-20230220_with price.csv"
OUT_FILE = Path(__file__).parent / "parts_catalog.json"

UNTRUSTED_MFR_CODES = {"", "VARIOUS", "UNKNOWN"}
MIN_FAMILY_PREFIX_LEN = 5


def load_pairs():
    if not CSV_FILE.exists():
        sys.exit(f"Error: {CSV_FILE.name} not found — run from the project directory.")
    pairs = set()
    with CSV_FILE.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            mfr = (row.get("Manufact.") or "").strip()
            part = (row.get("Manufacturer Part Number") or "").strip()
            if mfr and part:
                pairs.add((mfr, part))
    return pairs


def norm_for_dedup(mfr, part):
    """Aggressive normalization used only to detect pure-formatting duplicates."""
    def squash(s):
        s = s.upper()
        return re.sub(r"[\s\-_./]", "", s)
    return squash(mfr) + "||" + squash(part)


def family_prefix(part):
    """Strip a trailing numeric/length/serial suffix (incl. separators/'=')."""
    return re.sub(r"[\d.,=]+$", "", part).strip("-_./ =")


def build_catalog(pairs):
    # ── Stage 1: fold pure-formatting duplicates onto one canonical raw key ──
    dedup_groups = defaultdict(list)
    for mfr, part in pairs:
        dedup_groups[norm_for_dedup(mfr, part)].append((mfr, part))

    # canonical raw key per pair = shortest (mfr, part) string in its dedup group
    canonical_of = {}
    for group in dedup_groups.values():
        rep = min(group, key=lambda mp: len(mp[0]) + len(mp[1]))
        for mp in group:
            canonical_of[mp] = rep

    canonical_pairs = set(canonical_of.values())

    # ── Stage 2: cluster canonical pairs into part-families ──
    fam_groups = defaultdict(list)
    for mfr, part in canonical_pairs:
        if mfr in UNTRUSTED_MFR_CODES:
            continue
        base = family_prefix(part)
        if len(base) < MIN_FAMILY_PREFIX_LEN:
            continue
        fam_groups[(mfr, base)].append((mfr, part))

    representative_of = {p: p for p in canonical_pairs}  # default: singleton
    families_detail = {}
    for (mfr, base), members in fam_groups.items():
        if len(members) < 2:
            continue
        rep = min(members, key=lambda mp: (len(mp[1]), mp[1]))
        for mp in members:
            representative_of[mp] = rep
        rep_key = f"{rep[0]}||{rep[1]}"
        families_detail[rep_key] = sorted(f"{m[0]}||{m[1]}" for m in members if m != rep)

    # ── Stage 3: flatten to a lookup keyed by every ORIGINAL raw pair ──
    lookup = {}
    for mp in pairs:
        canon = canonical_of[mp]
        rep = representative_of[canon]
        lookup[f"{mp[0]}||{mp[1]}"] = f"{rep[0]}||{rep[1]}"

    return lookup, families_detail


def main():
    pairs = load_pairs()
    lookup, families_detail = build_catalog(pairs)

    representatives = set(lookup.values())
    family_member_count = sum(len(v) for v in families_detail.values())

    catalog = {
        "total_parts": len(pairs),
        "representative_count": len(representatives),
        "families": len(families_detail),
        "family_members": family_member_count,
        "lookup": lookup,
        "families_detail": families_detail,
    }
    OUT_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=None), encoding="utf-8")

    print(f"Parts catalog written to {OUT_FILE.name}")
    print(f"  total unique (mfr, part) pairs : {len(pairs)}")
    print(f"  representatives to enrich      : {len(representatives)}")
    print(f"  families (>1 member)           : {len(families_detail)}")
    print(f"  parts covered by families      : {family_member_count}")
    print(f"  enrichment work avoided         : {len(pairs) - len(representatives)} fewer lookups")


if __name__ == "__main__":
    main()
