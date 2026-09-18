#!/usr/bin/env python3
"""Regenerate empty_items.csv from current product_cache.json + parts_catalog.json state."""
import json, csv

cache = json.load(open("product_cache.json"))
catalog = json.load(open("parts_catalog.json"))
lookup = catalog["lookup"]  # raw_key -> representative_key
families_detail = catalog["families_detail"]  # representative -> [members]

mfr_col = "Manufact."
part_col = "Manufacturer Part Number"
desc_col = "comment"
value_col = "Net Order Value"
cur_col = "Currency"

def parse_val(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0

# CNY and EUR are both present in this dataset (generate_procurement.py reports
# them as two separate KPIs, never summed) -- track per-currency per key so we
# never silently add CNY + EUR into one meaningless number.
value_cny_by_key = {}
value_eur_by_key = {}
desc_by_key = {}
with open("KW07_Material status-20230220_with price.csv", encoding="utf-8-sig", newline="") as f:
    for row in csv.DictReader(f):
        mfr = (row.get(mfr_col) or "").strip()
        part = (row.get(part_col) or "").strip()
        cur = (row.get(cur_col) or "").strip()
        val = parse_val(row.get(value_col) or "")
        key = f"{mfr}||{part}"
        if cur == "CNY":
            value_cny_by_key[key] = value_cny_by_key.get(key, 0) + val
        elif cur == "EUR":
            value_eur_by_key[key] = value_eur_by_key.get(key, 0) + val
        if key not in desc_by_key:
            desc_by_key[key] = (row.get(desc_col) or "").strip()

# group raw keys by representative
reps_to_members = {}
for raw_key, rep_key in lookup.items():
    reps_to_members.setdefault(rep_key, []).append(raw_key)

rows = []
for rep_key, members in reps_to_members.items():
    entry = cache.get(rep_key)
    if entry and (entry.get("i") or entry.get("ib")):
        continue  # has image, skip
    mfr, part = rep_key.split("||", 1)
    total_cny = sum(value_cny_by_key.get(m, 0) for m in members)
    total_eur = sum(value_eur_by_key.get(m, 0) for m in members)
    desc = desc_by_key.get(rep_key, "") or (desc_by_key.get(members[0], "") if members else "")
    family_members = len(members) - 1  # sharing count excluding self
    query = f"{mfr} {part}".strip()
    rows.append({
        "manufacturer": mfr,
        "part_number": part,
        "description": desc,
        "total_order_value_cny": round(total_cny, 2),
        "total_order_value_eur": round(total_eur, 2),
        "family_members_sharing_this_image": family_members,
        "search_google_images": f"https://www.google.com/search?tbm=isch&q={query.replace(' ', '+')}",
    })

# Sort by CNY value primarily (98%+ of total spend is CNY-denominated),
# falling back to EUR value for the handful of EUR-only rows.
rows.sort(key=lambda r: -(r["total_order_value_cny"] or r["total_order_value_eur"] * 8.5))

with open("empty_items.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=["manufacturer","part_number","description","total_order_value_cny","total_order_value_eur","family_members_sharing_this_image","search_google_images"])
    w.writeheader()
    w.writerows(rows)

total_cny = sum(r["total_order_value_cny"] for r in rows)
total_eur = sum(r["total_order_value_eur"] for r in rows)
print(f"Wrote empty_items.csv: {len(rows)} rows")
print(f"Total order value represented: CNY {total_cny:,.2f} + EUR {total_eur:,.2f} (kept separate, not summed)")
