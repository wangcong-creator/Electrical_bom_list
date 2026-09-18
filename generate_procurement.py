#!/usr/bin/env python3
"""
generate_procurement.py — BBAC Procurement Order Tracking Dashboard
Reads : KW07_Material status-20230220_with price.csv  (45 850 rows)
Writes: procurement_dashboard.html  (self-contained, open in any browser)
Usage : python generate_procurement.py
"""

import pandas as pd
import json
import sys
from pathlib import Path
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════════════
# 1  Load
# ══════════════════════════════════════════════════════════════════════════════
CSV   = "KW07_Material status-20230220_with price.csv"
CACHE = Path("product_cache.json")
try:
    df = pd.read_csv(CSV, encoding="utf-8-sig", on_bad_lines="skip")
except FileNotFoundError:
    sys.exit(f"Error: {CSV} not found — run from the project directory.")

# Load product cache (title, description, image url, source url) keyed by "MFR||PARTNO".
# NOTE: base64 image blobs ("ib") are intentionally dropped here — images are now served
# as small local files under images/thumb|full/ (see export_images.py). Embedding "ib"
# directly in the page was tried and produced a 303 MB HTML file; never re-add it here.
if CACHE.exists():
    try:
        raw_cache = json.loads(CACHE.read_text(encoding="utf-8"))
        product_cache = {
            k: {f: v[f] for f in ("t", "d", "i", "u", "source", "inherited_from") if v.get(f)}
            for k, v in raw_cache.items()
            if v and (v.get("t") or v.get("i") or v.get("d"))
        }
    except Exception:
        product_cache = {}
else:
    product_cache = {}
print(f"Product cache: {len(product_cache)} enriched entries")

# ══════════════════════════════════════════════════════════════════════════════
# 2  Clean & parse
# ══════════════════════════════════════════════════════════════════════════════
def parse_val(v):
    try:    return round(float(str(v).replace(",", "")), 2)
    except: return 0.0

def to_yyyymmdd(d):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:   return int(datetime.strptime(str(d).strip(), fmt).strftime("%Y%m%d"))
        except: pass
    return 0

# Order number: pandas reads as float (1750035487.0) → convert via int first
df["order"] = df["order"].fillna(0).astype("Int64").astype(str).str.replace(r"\.0$","",regex=True).str.strip()

str_cols = ["Manufacture", "Manufact.", "comment",
            "Manufacturer Part Number", "Goods receipt status",
            "Account Assignment", "Purchasing Group", "Currency"]
for c in str_cols:
    df[c] = df[c].fillna("").astype(str).str.strip()

for c in ["Planned Quantity", "Delivered Quantity", "Open Delivery Quantity"]:
    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

df["value"]     = df["Net Order Value"].apply(parse_val)
df["plan_dt"]   = df["Planned delivery date"].apply(to_yyyymmdd)
df["create_dt"] = df["Creation Date"].apply(to_yyyymmdd)

# ══════════════════════════════════════════════════════════════════════════════
# 3  Lookup tables  (indices saved in each row → compact JSON)
# ══════════════════════════════════════════════════════════════════════════════
mfrs     = sorted(df["Manufact."].replace("", None).dropna().unique().tolist())
projs    = sorted(df["Account Assignment"].replace("", None).dropna().unique().tolist())
pgroups  = sorted(df["Purchasing Group"].replace("", None).dropna().unique().tolist())

# Company full name per manufacturer code
companies = {}
for code, grp in df[df["Manufact."] != ""].groupby("Manufact."):
    companies[code] = grp["Manufacture"].mode().iloc[0] if len(grp) else code
company_list = [companies.get(m, m) for m in mfrs]

mfr_idx  = {m: i for i, m in enumerate(mfrs)}
proj_idx = {p: i for i, p in enumerate(projs)}
pg_idx   = {p: i for i, p in enumerate(pgroups)}

# ══════════════════════════════════════════════════════════════════════════════
# 4  Build compact row arrays
#    [order, status, mfr_i, comment60, partno40, qty, deliv, open, pd, cd, proj_i, pg_i, val, cur]
# ══════════════════════════════════════════════════════════════════════════════
records = []
for _, r in df.iterrows():
    cur = "C" if r["Currency"] == "CNY" else "E"
    records.append([
        r["order"],
        r["Goods receipt status"],
        mfr_idx.get(r["Manufact."], -1),
        r["comment"][:60].strip(),
        r["Manufacturer Part Number"].strip(),  # NOT truncated: must match enrich_products.py's ckey() and the image slug exactly
        int(r["Planned Quantity"]),
        int(r["Delivered Quantity"]),
        int(r["Open Delivery Quantity"]),
        r["plan_dt"],
        r["create_dt"],
        proj_idx.get(r["Account Assignment"], -1),
        pg_idx.get(r["Purchasing Group"], -1),
        r["value"],
        cur,
    ])

# ══════════════════════════════════════════════════════════════════════════════
# 5  Aggregate stats
# ══════════════════════════════════════════════════════════════════════════════
total_orders  = int(df["order"].nunique())
total_rows    = int(len(df))
open_count    = int((df["Goods receipt status"] == "O").sum())
closed_count  = int((df["Goods receipt status"] == "X").sum())
total_cny     = round(float(df[df.Currency == "CNY"]["value"].sum()), 0)
total_eur     = round(float(df[df.Currency == "EUR"]["value"].sum()), 0)

# Top 20 manufacturers by spend (exclude empty/blank Manufact.)
df_mfr = df[df["Manufact."].str.strip() != ""]
top20 = (df_mfr.groupby("Manufact.")["value"].sum()
               .sort_values(ascending=False).head(20))
spend_labels = top20.index.tolist()
spend_values = [round(float(v), 0) for v in top20.values]

# Monthly creation trend
df["create_month"] = df["create_dt"].apply(
    lambda d: f"{str(d)[:4]}-{str(d)[4:6]}" if d > 0 else "")
monthly = (df[df["create_month"] != ""].groupby("create_month")
             .agg(rows=("value", "count"), spend=("value", "sum"))
             .reset_index().sort_values("create_month"))
monthly_labels  = monthly["create_month"].tolist()
monthly_rows    = [int(v) for v in monthly["rows"]]
monthly_spend   = [round(float(v), 0) for v in monthly["spend"]]

# Open items by manufacturer (top 15, exclude empty)
open_df  = df[(df["Goods receipt status"] == "O") & (df["Manufact."].str.strip() != "")]
open_top = open_df.groupby("Manufact.").size().sort_values(ascending=False).head(15)
open_mfr_labels = open_top.index.tolist()
open_mfr_values = [int(v) for v in open_top.values]

# Date range for filter
dt_min = int(df[df["create_dt"] > 0]["create_dt"].min())
dt_max = int(df[df["create_dt"] > 0]["create_dt"].max())

def dt_str(d):
    s = str(d).zfill(8)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

stats = {
    "total_orders":  total_orders,
    "total_rows":    total_rows,
    "open_count":    open_count,
    "closed_count":  closed_count,
    "total_cny":     total_cny,
    "total_eur":     total_eur,
    "spend_labels":  spend_labels,
    "spend_values":  spend_values,
    "monthly_labels": monthly_labels,
    "monthly_rows":  monthly_rows,
    "monthly_spend": monthly_spend,
    "open_mfr_labels": open_mfr_labels,
    "open_mfr_values": open_mfr_values,
    "dt_min": dt_str(dt_min),
    "dt_max": dt_str(dt_max),
}

# ══════════════════════════════════════════════════════════════════════════════
# 6  HTML template
# ══════════════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>BBAC Procurement Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f172a;--card:#1e293b;--card2:#263347;
  --border:#334155;--text:#e2e8f0;--muted:#94a3b8;
  --primary:#3b82f6;--success:#22c55e;--warn:#f59e0b;
  --danger:#ef4444;--accent:#818cf8;--rad:7px;
  --sh:0 1px 3px rgba(0,0,0,.4);
}
html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  background:var(--bg);color:var(--text);font-size:14px;line-height:1.5;min-height:100vh}

/* ── Header ── */
.hdr{
  background:linear-gradient(135deg,#020617 0%,#0f172a 60%,#1e1b4b 100%);
  border-bottom:1px solid var(--border);
  padding:16px 24px;display:flex;align-items:center;justify-content:space-between;
}
.hdr-left h1{font-size:18px;font-weight:700;letter-spacing:-.3px;color:#f1f5f9}
.hdr-left h1 span{color:var(--primary)}
.hdr-left p{font-size:12px;color:var(--muted);margin-top:2px}
.hdr-right{font-size:12px;color:var(--muted);text-align:right}

/* ── KPI Strip ── */
.kpi-strip{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));
  gap:10px;padding:14px 24px;
}
.kpi{
  background:var(--card);border-radius:var(--rad);border:1px solid var(--border);
  padding:14px 16px;position:relative;overflow:hidden;
}
.kpi::before{content:'';position:absolute;top:0;left:0;width:4px;height:100%}
.kpi.blue::before{background:var(--primary)}
.kpi.green::before{background:var(--success)}
.kpi.amber::before{background:var(--warn)}
.kpi.purple::before{background:var(--accent)}
.kpi.red::before{background:var(--danger)}
.kpi-val{font-size:22px;font-weight:700;line-height:1;letter-spacing:-.5px}
.kpi-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-top:4px}
.kpi-sub{font-size:11px;color:var(--muted);margin-top:2px}

/* ── Tabs ── */
.tabs{display:flex;border-bottom:1px solid var(--border);padding:0 24px;background:var(--card)}
.tab{
  padding:11px 18px;font-size:13px;font-weight:500;cursor:pointer;
  border-bottom:2px solid transparent;color:var(--muted);transition:all .15s;
}
.tab.active{color:var(--primary);border-color:var(--primary)}
.tab:hover:not(.active){color:var(--text)}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* ── Filter Bar ── */
.fbar{
  position:sticky;top:0;z-index:90;
  background:var(--card2);border-bottom:1px solid var(--border);
  padding:9px 24px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;
}
.fbar input[type=search],.fbar select,.fbar input[type=date]{
  background:var(--card);border:1px solid var(--border);border-radius:var(--rad);
  color:var(--text);padding:5px 9px;font-size:12px;outline:none;
}
.fbar input[type=search]{flex:1 1 200px;min-width:150px}
.fbar input:focus,.fbar select:focus{border-color:var(--primary)}
.fbar select option{background:var(--card)}
.sbg{display:flex;gap:0}
.sbg button{
  background:var(--card);border:1px solid var(--border);color:var(--muted);
  padding:5px 11px;font-size:12px;cursor:pointer;transition:all .15s;margin-left:-1px;
}
.sbg button:first-child{border-radius:var(--rad) 0 0 var(--rad);margin-left:0}
.sbg button:last-child{border-radius:0 var(--rad) var(--rad) 0}
.sbg button.on{background:var(--primary);color:#fff;border-color:var(--primary);z-index:1}
.fbar-sep{width:1px;height:22px;background:var(--border)}
.exp-btn{
  margin-left:auto;background:var(--primary);color:#fff;border:none;
  border-radius:var(--rad);padding:6px 14px;font-size:12px;cursor:pointer;
  white-space:nowrap;font-weight:500;
}
.exp-btn:hover{background:#2563eb}
.res-bar{
  padding:6px 24px;font-size:12px;color:var(--muted);
  background:var(--bg);border-bottom:1px solid var(--border);
}

/* ── Virtual Scroll Table ── */
.tbl-outer{position:relative}
.tbl-head{
  overflow:hidden;border-bottom:2px solid var(--border);
  background:var(--card2);position:sticky;top:0;z-index:80;
}
.tbl-head table{width:100%;border-collapse:collapse;table-layout:fixed}
.tbl-head th{
  padding:8px 10px;font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.5px;color:var(--muted);text-align:left;white-space:nowrap;
  cursor:pointer;user-select:none;
}
.tbl-head th:hover{color:var(--text)}
.tbl-head th.sort-asc::after{content:" ↑";color:var(--primary)}
.tbl-head th.sort-desc::after{content:" ↓";color:var(--primary)}
.scroll-wrap{
  height:520px;overflow-y:auto;overflow-x:hidden;
  background:var(--bg);
}
.scroll-inner{position:relative;width:100%}
.virt-table{
  position:absolute;width:100%;border-collapse:collapse;table-layout:fixed;
  background:transparent;
}
.virt-table td{
  padding:7px 10px;font-size:12px;vertical-align:middle;
  border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.virt-table tr:hover td{background:var(--card2)}
/* col widths */
.c0{width:70px}.c1{width:110px}.c2{width:90px}.c3{width:190px}
.c4{width:120px}.c5{width:48px}.c6{width:48px}.c7{width:48px}
.c8{width:88px}.c9{width:88px}.c10{width:90px}.c11{width:55px}.c12{width:82px}
.c14{width:82px}.c13{width:46px;text-align:center}

/* ── Badges ── */
.badge{display:inline-block;padding:2px 7px;border-radius:9999px;
       font-size:11px;font-weight:700;letter-spacing:.3px}
.bx{background:#14532d;color:#86efac}
.bo{background:#78350f;color:#fcd34d}
.cur-c{color:#94a3b8;font-size:11px}
.cur-e{color:var(--accent);font-size:11px}

/* ── Copy button ── */
.cp{border:none;background:transparent;color:var(--muted);cursor:pointer;
    padding:1px 4px;border-radius:3px;font-size:12px;opacity:0;transition:opacity .15s}
td:hover .cp,.cp.ok{opacity:1}
.cp:hover{background:var(--border)}
.cp.ok{color:var(--success)}

/* ── Analytics Tab ── */
.analytics-grid{
  display:grid;grid-template-columns:1fr 1fr;
  gap:16px;padding:20px 24px;
}
.analytics-grid.wide{grid-template-columns:1fr}
.chart-card{
  background:var(--card);border:1px solid var(--border);border-radius:var(--rad);
  padding:16px;
}
.chart-card h3{font-size:13px;font-weight:600;color:var(--muted);
               text-transform:uppercase;letter-spacing:.4px;margin-bottom:12px}
canvas{display:block;max-width:100%}

/* ── Toast ── */
#toast{
  position:fixed;bottom:20px;right:20px;background:#1e293b;border:1px solid var(--border);
  color:var(--text);padding:9px 16px;border-radius:var(--rad);font-size:12px;
  opacity:0;transform:translateY(6px);transition:opacity .2s,transform .2s;
  pointer-events:none;z-index:9999;
}
#toast.show{opacity:1;transform:translateY(0)}

/* ── Scrollbar ── */
.scroll-wrap::-webkit-scrollbar{width:7px}
.scroll-wrap::-webkit-scrollbar-track{background:var(--bg)}
.scroll-wrap::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}

/* ── Product Modal ── */
.modal-overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
  z-index:1000;align-items:center;justify-content:center;
}
.modal-overlay.open{display:flex}
.modal-box{
  background:var(--card);border:1px solid var(--border);border-radius:10px;
  width:min(560px,94vw);max-height:88vh;overflow-y:auto;
  box-shadow:0 20px 60px rgba(0,0,0,.6);
}
.modal-hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:14px 18px;border-bottom:1px solid var(--border);
}
.modal-hdr h2{font-size:13px;font-weight:700;color:var(--text);
              text-transform:uppercase;letter-spacing:.3px}
.modal-close{
  background:none;border:none;color:var(--muted);font-size:20px;
  cursor:pointer;line-height:1;padding:2px 6px;border-radius:4px;
}
.modal-close:hover{background:var(--border);color:var(--text)}
.modal-body{padding:18px}
.prod-img-wrap{
  background:var(--card2);border-radius:6px;
  display:flex;align-items:center;justify-content:center;
  height:220px;margin-bottom:14px;overflow:hidden;border:1px solid var(--border);
}
.prod-img-wrap img{max-width:100%;max-height:100%;object-fit:contain;border-radius:4px}
.prod-img-wrap .no-img{color:var(--muted);font-size:13px;text-align:center;padding:20px}
.prod-title{font-size:15px;font-weight:600;color:var(--text);margin-bottom:6px;line-height:1.4}
.prod-part{font-size:11px;color:var(--muted);margin-bottom:10px;font-family:monospace}
.prod-desc{font-size:13px;color:#94a3b8;line-height:1.6;margin-bottom:14px}
.prod-source{
  font-size:11px;margin-bottom:16px;display:block;
  color:var(--primary);text-decoration:none;word-break:break-all;
}
.prod-source:hover{text-decoration:underline}
.prod-actions{display:flex;flex-wrap:wrap;gap:8px}
.prod-btn{
  display:inline-flex;align-items:center;gap:5px;
  padding:7px 13px;border-radius:6px;font-size:12px;font-weight:500;
  border:1px solid var(--border);background:var(--card2);color:var(--text);
  text-decoration:none;cursor:pointer;transition:all .15s;
}
.prod-btn:hover{border-color:var(--primary);color:var(--primary)}
/* Info icon in table */
.info-btn{
  border:none;background:none;cursor:pointer;font-size:14px;
  padding:2px 4px;border-radius:3px;line-height:1;opacity:.5;
  transition:opacity .15s;
}
.info-btn:hover,.info-btn.has-data{opacity:1}
.info-btn.has-data{color:#38bdf8}
.row-thumb{
  width:26px;height:26px;object-fit:contain;border-radius:3px;cursor:pointer;
  background:var(--card2);border:1px solid var(--border);vertical-align:middle;
}
.row-thumb:hover{border-color:var(--primary)}

/* ── Responsive ── */
@media(max-width:900px){
  .analytics-grid{grid-template-columns:1fr}
  .kpi-strip{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <div class="hdr-left">
    <h1>&#x26A1; BBAC <span>Procurement</span> Dashboard</h1>
    <p>KW07 &middot; Material Status as of 2023-02-20 &middot; <span id="hdr-info">loading&hellip;</span></p>
  </div>
  <div class="hdr-right" id="hdr-right"></div>
</div>

<!-- KPI Strip -->
<div class="kpi-strip" id="kpi-strip"></div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" data-tab="orders">&#128230; Orders</div>
  <div class="tab" data-tab="analytics">&#128200; Analytics</div>
</div>

<!-- Orders Tab -->
<div class="tab-panel active" id="panel-orders">
  <div class="fbar" id="fbar">
    <input type="search" id="search" placeholder="&#128269; Search description, part #, order…"/>
    <select id="sel-mfr"><option value="">All Manufacturers</option></select>
    <select id="sel-proj"><option value="">All Projects</option></select>
    <select id="sel-pg"><option value="">All Purch. Groups</option></select>
    <div class="fbar-sep"></div>
    <div class="sbg" id="st-btns">
      <button class="on" data-v="all">All</button>
      <button data-v="X">&#x2713; Closed</button>
      <button data-v="O">&#x25CB; Open</button>
    </div>
    <select id="sel-cur">
      <option value="">All Currency</option>
      <option value="C">CNY</option>
      <option value="E">EUR</option>
    </select>
    <div class="fbar-sep"></div>
    <label style="font-size:12px;color:var(--muted)">From</label>
    <input type="date" id="dt-from"/>
    <label style="font-size:12px;color:var(--muted)">To</label>
    <input type="date" id="dt-to"/>
    <button class="exp-btn" id="exp-btn">&#x2193; Export CSV</button>
  </div>
  <div class="res-bar" id="res-bar">Loading&hellip;</div>
  <div class="tbl-outer">
    <div class="tbl-head">
      <table><colgroup>
        <col class="c0"/><col class="c1"/><col class="c2"/><col class="c3"/>
        <col class="c4"/><col class="c5"/><col class="c6"/><col class="c7"/>
        <col class="c8"/><col class="c9"/><col class="c10"/><col class="c11"/>
        <col class="c12"/><col class="c14"/><col class="c13"/>
      </colgroup><thead><tr>
        <th data-col="1">Status</th>
        <th data-col="0">Order #</th>
        <th data-col="2">Manufacturer</th>
        <th data-col="3">Description</th>
        <th data-col="4">Part #</th>
        <th data-col="5" style="text-align:right">Qty</th>
        <th data-col="6" style="text-align:right">Dlvd</th>
        <th data-col="7" style="text-align:right">Open</th>
        <th data-col="8">Plan Date</th>
        <th data-col="9">Create Date</th>
        <th data-col="10">Project</th>
        <th data-col="13">Cur</th>
        <th data-col="12" style="text-align:right">Value</th>
        <th style="text-align:right">Unit Val</th>
        <th style="text-align:center">&#128269;</th>
      </tr></thead></table>
    </div>
    <div class="scroll-wrap" id="scroll-wrap">
      <div class="scroll-inner" id="scroll-inner">
        <table class="virt-table" id="virt-table">
          <colgroup>
            <col class="c0"/><col class="c1"/><col class="c2"/><col class="c3"/>
            <col class="c4"/><col class="c5"/><col class="c6"/><col class="c7"/>
            <col class="c8"/><col class="c9"/><col class="c10"/><col class="c11"/>
            <col class="c12"/><col class="c14"/><col class="c13"/>
          </colgroup>
          <tbody id="virt-body"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- Analytics Tab -->
<div class="tab-panel" id="panel-analytics">
  <div class="analytics-grid" style="grid-template-columns:2fr 1fr">
    <div class="chart-card">
      <h3>Top 20 Manufacturers by Spend</h3>
      <canvas id="c-bar" width="700" height="520"></canvas>
    </div>
    <div class="chart-card">
      <h3>Order Status</h3>
      <canvas id="c-donut" width="320" height="320"></canvas>
      <div id="donut-legend" style="margin-top:12px;font-size:12px;text-align:center"></div>
    </div>
  </div>
  <div class="analytics-grid" style="grid-template-columns:1fr 1fr">
    <div class="chart-card">
      <h3>Monthly Order Volume</h3>
      <canvas id="c-trend" width="600" height="260"></canvas>
    </div>
    <div class="chart-card">
      <h3>Open Items by Manufacturer (Top 15)</h3>
      <canvas id="c-open" width="600" height="260"></canvas>
    </div>
  </div>
</div>

<!-- Product Modal -->
<div class="modal-overlay" id="prod-modal" onclick="if(event.target===this)closeModal()">
  <div class="modal-box">
    <div class="modal-hdr">
      <h2 id="modal-mfr-label">Product Info</h2>
      <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<div id="toast"></div>

<script>
// ── Embedded data ──────────────────────────────────────────────────────────
const MFRS     = __MFRS__;
const COMPANIES= __COMPANIES__;
const PROJS    = __PROJS__;
const PGS      = __PGS__;
const DATA     = __DATA__;
const STATS    = __STATS__;
const PROD     = __PROD__;   // product cache: {"MFR||PART":{t,d,i,u}}

// ── Column field indices ───────────────────────────────────────────────────
const FO=0,FS=1,FM=2,FC=3,FP=4,FQ=5,FD=6,FR=7,FPD=8,FCD=9,FJ=10,FG=11,FV=12,FCU=13;

// ── State ─────────────────────────────────────────────────────────────────
const state = {
  search:"", mfr:-1, proj:-1, pg:-1,
  status:"all", cur:"", dtFrom:0, dtTo:99999999,
  sortCol:-1, sortDir:1,
};
let filteredData = DATA.slice();

// ── Helpers ───────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const esc = s => String(s==null?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
// Must exactly match slug() in export_images.py so row lookups hit the right local file.
const slug = (mfr,part) => (mfr+"_"+part).replace(/[^A-Za-z0-9]+/g,"_").replace(/^_+|_+$/g,"").slice(0,120) || "unnamed";

function fmtDate(d) {
  const s = String(d).padStart(8,"0");
  return s.slice(0,4)+"-"+s.slice(4,6)+"-"+s.slice(6,8);
}
function dateToInt(s) {
  if(!s)return 0;
  const d=new Date(s);
  return isNaN(d)?0:parseInt(d.toISOString().slice(0,10).replace(/-/g,""));
}
function fmtVal(v) {
  if(v===0)return "—";
  return v>=1e6 ? (v/1e6).toFixed(2)+"M" : v>=1e3 ? (v/1e3).toFixed(1)+"K" : v.toFixed(0);
}
function fmtValFull(v,cur) {
  const sym = cur==="C"?"¥":"€";
  return sym+Number(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
}

// ── Boot ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded",()=>{
  renderKPIs();
  buildSelects();
  attachListeners();
  applyFilters();
  drawAllCharts();
});

// ── KPI Cards ─────────────────────────────────────────────────────────────
function renderKPIs() {
  const openPct = (STATS.open_count/STATS.total_rows*100).toFixed(1);
  $("kpi-strip").innerHTML = [
    {v:STATS.total_orders.toLocaleString(),      l:"Unique Orders",  s:"purchase orders",    c:"blue"},
    {v:STATS.total_rows.toLocaleString(),         l:"Line Items",     s:"procurement lines",  c:"purple"},
    {v:STATS.open_count.toLocaleString(),         l:"Open Items",     s:openPct+"% of total", c:"amber"},
    {v:"¥"+fmtVal(STATS.total_cny),              l:"Total CNY Spend",s:STATS.total_cny.toLocaleString("en-US",{maximumFractionDigits:0})+" CNY",c:"blue"},
    {v:"€"+fmtVal(STATS.total_eur),              l:"Total EUR Spend",s:STATS.total_eur.toLocaleString("en-US",{maximumFractionDigits:0})+" EUR",c:"purple"},
    {v:MFRS.length.toLocaleString(),             l:"Manufacturers",  s:"unique vendors",     c:"green"},
  ].map(k=>`<div class="kpi ${k.c}">
    <div class="kpi-val">${k.v}</div>
    <div class="kpi-lbl">${k.l}</div>
    <div class="kpi-sub">${k.s}</div>
  </div>`).join("");
  $("hdr-info").textContent=`${STATS.total_rows.toLocaleString()} lines · ${STATS.total_orders.toLocaleString()} orders`;
  $("hdr-right").innerHTML=`KPI as of 2023-02-20<br>CNY: ¥${fmtVal(STATS.total_cny)} &nbsp; EUR: €${fmtVal(STATS.total_eur)}`;
}

// ── Selects ───────────────────────────────────────────────────────────────
function buildSelects() {
  const sm=$("sel-mfr");
  MFRS.forEach((m,i)=>{ const o=new Option(m,i); sm.appendChild(o); });

  const sp=$("sel-proj");
  PROJS.forEach((p,i)=>{ const o=new Option(p,i); sp.appendChild(o); });

  const sg=$("sel-pg");
  PGS.forEach((g,i)=>{ const o=new Option(g,i); sg.appendChild(o); });

  $("dt-from").value = STATS.dt_min;
  $("dt-to").value   = STATS.dt_max;
  state.dtFrom = dateToInt(STATS.dt_min);
  state.dtTo   = dateToInt(STATS.dt_max);
}

// ── Listeners ─────────────────────────────────────────────────────────────
function attachListeners() {
  let st; // debounce timer
  $("search").addEventListener("input",e=>{
    state.search=e.target.value.trim().toLowerCase();
    clearTimeout(st); st=setTimeout(applyFilters,180);
  });
  $("sel-mfr").addEventListener("change",e=>{state.mfr=e.target.value===""?-1:+e.target.value;applyFilters()});
  $("sel-proj").addEventListener("change",e=>{state.proj=e.target.value===""?-1:+e.target.value;applyFilters()});
  $("sel-pg").addEventListener("change",e=>{state.pg=e.target.value===""?-1:+e.target.value;applyFilters()});
  $("sel-cur").addEventListener("change",e=>{state.cur=e.target.value;applyFilters()});
  $("dt-from").addEventListener("change",e=>{state.dtFrom=dateToInt(e.target.value)||0;applyFilters()});
  $("dt-to").addEventListener("change",e=>{state.dtTo=dateToInt(e.target.value)||99999999;applyFilters()});
  $("exp-btn").addEventListener("click",exportCSV);

  // Status buttons
  document.querySelectorAll("#st-btns button").forEach(btn=>{
    btn.addEventListener("click",()=>{
      document.querySelectorAll("#st-btns button").forEach(b=>b.classList.remove("on"));
      btn.classList.add("on"); state.status=btn.dataset.v; applyFilters();
    });
  });

  // Tabs
  document.querySelectorAll(".tab").forEach(tab=>{
    tab.addEventListener("click",()=>{
      document.querySelectorAll(".tab").forEach(t=>t.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p=>p.classList.remove("active"));
      tab.classList.add("active");
      $("panel-"+tab.dataset.tab).classList.add("active");
      if(tab.dataset.tab==="analytics") drawAllCharts();
    });
  });

  // Column sort
  document.querySelectorAll(".tbl-head th[data-col]").forEach(th=>{
    th.addEventListener("click",()=>{
      const col=+th.dataset.col;
      if(state.sortCol===col) state.sortDir*=-1; else{state.sortCol=col;state.sortDir=1;}
      document.querySelectorAll(".tbl-head th").forEach(t=>t.classList.remove("sort-asc","sort-desc"));
      th.classList.add(state.sortDir===1?"sort-asc":"sort-desc");
      sortData(); renderVisible(0);
    });
  });

  // Virtual scroll
  $("scroll-wrap").addEventListener("scroll",e=>renderVisible(e.target.scrollTop));
}

// ── Filter ────────────────────────────────────────────────────────────────
function applyFilters() {
  const s=state;
  filteredData = DATA.filter(r=>{
    if(s.status!=="all" && r[FS]!==s.status) return false;
    if(s.mfr>=0  && r[FM]!==s.mfr)  return false;
    if(s.proj>=0 && r[FJ]!==s.proj) return false;
    if(s.pg>=0   && r[FG]!==s.pg)   return false;
    if(s.cur && r[FCU]!==s.cur) return false;
    if(r[FCD]<s.dtFrom || r[FCD]>s.dtTo) return false;
    if(s.search){
      const h=(r[FC]+r[FP]+r[FO]).toLowerCase();
      if(!h.includes(s.search)) return false;
    }
    return true;
  });
  if(state.sortCol>=0) sortData();
  const totalVal = filteredData.reduce((a,r)=>a+r[FV],0);
  const openCnt  = filteredData.filter(r=>r[FS]==="O").length;
  $("res-bar").textContent =
    `Showing ${filteredData.length.toLocaleString()} of ${DATA.length.toLocaleString()} lines`+
    ` · Open: ${openCnt.toLocaleString()} · Total Value: ${fmtValFull(totalVal,"C")}`;
  $("scroll-wrap").scrollTop=0;
  $("scroll-inner").style.height=(filteredData.length*ROW_H)+"px";
  renderVisible(0);
}

// ── Sort ──────────────────────────────────────────────────────────────────
function sortData() {
  const col=state.sortCol, dir=state.sortDir;
  filteredData.sort((a,b)=>{
    const av=a[col], bv=b[col];
    if(typeof av==="number"&&typeof bv==="number") return (av-bv)*dir;
    return String(av).localeCompare(String(bv))*dir;
  });
}

// ── Virtual Scroll ────────────────────────────────────────────────────────
const ROW_H=40, BUFFER=12;
function renderVisible(scrollTop) {
  const wrap=$("scroll-wrap");
  const viewH=wrap.clientHeight;
  const start=Math.max(0,Math.floor(scrollTop/ROW_H)-BUFFER);
  const end=Math.min(filteredData.length,Math.ceil((scrollTop+viewH)/ROW_H)+BUFFER);
  $("virt-table").style.top=(start*ROW_H)+"px";
  $("virt-body").innerHTML=filteredData.slice(start,end).map(rowHtml).join("");
}

function rowHtml(r) {
  const st=r[FS]==="X"
    ? `<span class="badge bx">&#x2713; Closed</span>`
    : `<span class="badge bo">&#x25CB; Open</span>`;
  const mfr=MFRS[r[FM]]||"";
  const co=COMPANIES[r[FM]]||"";
  const cur=r[FCU]==="C"?`<span class="cur-c">CNY</span>`:`<span class="cur-e">EUR</span>`;
  const val=`<span title="${fmtValFull(r[FV],r[FCU])}">${fmtVal(r[FV])}</span>`;
  const cp=v=>v?`<button class="cp" onclick="copyText(this,'${esc(v).replace(/'/g,"\\'")}')">&#x2398;</button>`:"";
  const pkey=mfr+"||"+r[FP];
  const hasData=!!(PROD[pkey] && (PROD[pkey].i||PROD[pkey].d||PROD[pkey].t));
  const emfr=esc(mfr).replace(/'/g,"\\'"), epart=esc(r[FP]).replace(/'/g,"\\'"),
        eco=esc(co).replace(/'/g,"\\'"), edesc=esc(r[FC]).replace(/'/g,"\\'");
  const infoBtn=`<img class="row-thumb" src="images/thumb/${slug(mfr,r[FP])}.webp" loading="lazy" alt=""
    onclick="openProduct('${emfr}','${epart}','${eco}','${edesc}',event)"
    onerror="thumbErr(this,'${emfr}','${epart}','${eco}','${edesc}')"
    title="${hasData?"View product info":"Search online"}">`;
  const unitVal = r[FQ]>0 ? fmtVal(r[FV]/r[FQ]) : "—";
  const unitFull = r[FQ]>0 ? fmtValFull(r[FV]/r[FQ],r[FCU]) : "—";
  return `<tr>
    <td>${st}</td>
    <td class="code" title="${esc(r[FO])}">${esc(r[FO])} ${cp(r[FO])}</td>
    <td title="${esc(co)}">${esc(mfr)}</td>
    <td title="${esc(r[FC])}">${esc(r[FC])}</td>
    <td title="${esc(r[FP])}">${esc(r[FP])} ${cp(r[FP])}</td>
    <td style="text-align:right">${r[FQ].toLocaleString()}</td>
    <td style="text-align:right">${r[FD].toLocaleString()}</td>
    <td style="text-align:right;color:${r[FR]>0?"var(--warn)":"var(--muted)"}">${r[FR].toLocaleString()}</td>
    <td>${fmtDate(r[FPD])}</td>
    <td>${fmtDate(r[FCD])}</td>
    <td title="${esc(PROJS[r[FJ]]||"")}">${esc(PROJS[r[FJ]]||"")}</td>
    <td>${cur}</td>
    <td style="text-align:right;font-weight:600">${val}</td>
    <td style="text-align:right;color:var(--muted)" title="${unitFull}">${unitVal}</td>
    <td style="text-align:center">${infoBtn}</td>
  </tr>`;
}

// ── Product Modal ─────────────────────────────────────────────────────────
// Row thumbnail has no local file yet (no source image, or export_images.py hasn't run
// for it) — fall back to the icon-only button so the row still tells you whether info exists.
function thumbErr(el, mfr, part, company, desc) {
  const key = mfr+"||"+part;
  const hasData = !!(PROD[key] && (PROD[key].i||PROD[key].d||PROD[key].t));
  const btn = document.createElement("button");
  btn.className = "info-btn"+(hasData?" has-data":"");
  btn.title = hasData ? "View product info" : "Search online";
  btn.innerHTML = hasData ? "&#128247;" : "&#128269;";
  btn.onclick = evt => openProduct(mfr, part, company, desc, evt);
  el.replaceWith(btn);
}

function openProduct(mfr, part, company, desc, evt) {
  if(evt){evt.stopPropagation();}
  const key = mfr+"||"+part;
  const p = PROD[key]||{};
  const hasImg = !!p.i;
  const hasDesc = !!p.d;
  const hasTitle = !!p.t;
  const hasAny = hasImg||hasDesc||hasTitle;

  const q = encodeURIComponent(mfr+" "+part);
  const qp = encodeURIComponent(part);

  // Try the local exported file first (images/full/<slug>.webp); if it 404s (not
  // exported yet), fall back to the original hotlinked URL, then a CORS-bypass proxy
  // of that URL, then a placeholder with a manual-search link.
  const localSrc = `images/full/${slug(mfr,part)}.webp`;
  const rawUrl = p.i || "";
  const proxyUrl = rawUrl ? "https://images.weserv.nl/?url="+encodeURIComponent(rawUrl)+"&maxage=7d" : "";
  const imgBlock =
    `<img src="${esc(localSrc)}" alt="${esc(part)}" referrerpolicy="no-referrer"
       onerror="imgError(this,'${rawUrl.replace(/'/g,"\\'")}','${proxyUrl.replace(/'/g,"\\'")}','${esc(q).replace(/'/g,"\\'")}')">`;

  const titleBlock = hasTitle
    ? `<div class="prod-title">${esc(p.t)}</div>`
    : (hasAny ? `` : `<div class="prod-title">${esc(desc||part)}</div>`);

  const descBlock = hasDesc
    ? `<div class="prod-desc">${esc(p.d)}</div>`
    : ``;

  const sourceBlock = p.u
    ? `<a class="prod-source" href="${esc(p.u)}" target="_blank" rel="noopener">&#128279; ${esc(p.u)}</a>`
    : ``;
  const SRC_LABEL = {mouser:"Mouser API",nexar:"Nexar/Octopart API",scrape:"web scrape (verified)",bing:"Bing Images (verified)"};
  const provenanceBlock = p.source
    ? `<div class="prod-part" style="margin-top:-4px">Source: ${esc(SRC_LABEL[p.source]||p.source)}${p.inherited_from?" &middot; shared from "+esc(p.inherited_from):""}</div>`
    : ``;

  const btns = [
    {label:"&#128269; RS Components", href:`https://uk.rs-online.com/web/c/?searchTerm=${qp}&redirect=y`},
    {label:"&#128269; Mouser",         href:`https://www.mouser.com/Search/Refine?Keyword=${qp}`},
    {label:"&#127757; Google",         href:`https://www.google.com/search?q=${q}`},
    {label:"&#128247; Google Images",  href:`https://images.google.com/search?tbm=isch&q=${q}`},
  ].map(b=>`<a class="prod-btn" href="${b.href}" target="_blank" rel="noopener">${b.label}</a>`).join("");

  $("modal-mfr-label").textContent = (company||mfr)+" — "+part;
  $("modal-body").innerHTML =
    `<div class="prod-img-wrap">${imgBlock}</div>
     <div class="prod-part">${esc(mfr)} &nbsp;/&nbsp; ${esc(part)}</div>
     ${titleBlock}${descBlock}${sourceBlock}${provenanceBlock}
     <div class="prod-actions">${btns}</div>`;
  $("prod-modal").classList.add("open");
}

function imgError(el, rawUrl, proxyUrl, q) {
  el._step = (el._step||0) + 1;
  if (el._step === 1 && rawUrl) {
    // Local file 404'd (not exported yet) — try the original hotlinked URL.
    el.src = rawUrl;
  } else if (el._step <= 2 && proxyUrl) {
    // That failed too (or there was no local/raw URL to try) — CORS-bypass proxy.
    el.src = proxyUrl;
  } else {
    // Nothing worked — placeholder with a manual-search link.
    el.parentNode.innerHTML =
      `<div class="no-img">&#128247; Image unavailable
       <br><small style="color:var(--muted)">Not exported yet, or hotlink removed</small>
       <br><br><a href="https://images.google.com/search?tbm=isch&q=${q}"
         target="_blank" rel="noopener"
         style="color:var(--primary);font-size:12px">&#128269; Search Google Images</a></div>`;
  }
}

function closeModal() {
  $("prod-modal").classList.remove("open");
}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal();});

// ── Copy ──────────────────────────────────────────────────────────────────
function copyText(btn,txt) {
  (navigator.clipboard||{writeText:()=>Promise.reject()}).writeText(txt).then(ok,ok);
  function ok(){
    btn.textContent="✓";btn.classList.add("ok");
    toast("Copied: "+txt);
    setTimeout(()=>{btn.textContent="⎘";btn.classList.remove("ok");},1500);
  }
}
let _tt;
function toast(msg){
  const t=$("toast");t.textContent=msg;t.classList.add("show");
  clearTimeout(_tt);_tt=setTimeout(()=>t.classList.remove("show"),2200);
}

// ── Export CSV ────────────────────────────────────────────────────────────
function exportCSV() {
  const HDR=["Order","Status","Manufacturer","Company","Description","Part #",
             "Qty Planned","Qty Delivered","Qty Open",
             "Planned Date","Creation Date","Project","Purch.Group","Currency","Net Value","Unit Value"];
  const rows=[HDR,...filteredData.map(r=>[
    r[FO],r[FS],MFRS[r[FM]]||"",COMPANIES[r[FM]]||"",r[FC],r[FP],
    r[FQ],r[FD],r[FR],fmtDate(r[FPD]),fmtDate(r[FCD]),
    PROJS[r[FJ]]||"",PGS[r[FG]]||"",r[FCU]==="C"?"CNY":"EUR",r[FV],
    r[FQ]>0?(r[FV]/r[FQ]).toFixed(2):"",
  ])].map(row=>row.map(v=>`"${String(v==null?"":v).replace(/"/g,'""')}"`).join(",")).join("\r\n");
  const blob=new Blob(["﻿"+rows],{type:"text/csv;charset=utf-8"});
  const a=Object.assign(document.createElement("a"),{href:URL.createObjectURL(blob),download:"procurement_filtered.csv"});
  a.click();URL.revokeObjectURL(a.href);
  toast(`Exported ${filteredData.length.toLocaleString()} rows`);
}

// ══════════════════════════════════════════════════════════════════════════
// Canvas Charts
// ══════════════════════════════════════════════════════════════════════════
function drawAllCharts() {
  drawHBar($("c-bar"), STATS.spend_labels, STATS.spend_values, "Spend (CNY)", "#3b82f6");
  drawDonut($("c-donut"), STATS.closed_count, STATS.open_count);
  drawLine($("c-trend"), STATS.monthly_labels, STATS.monthly_rows, "Orders Created per Month", "#818cf8");
  drawHBar($("c-open"), STATS.open_mfr_labels, STATS.open_mfr_values, "Open Items", "#f59e0b");
}

// Horizontal bar chart
function drawHBar(canvas, labels, values, xLabel, color) {
  const ctx=canvas.getContext("2d");
  const W=canvas.width, H=canvas.height;
  const maxV=Math.max(...values)||1;
  const n=labels.length;
  const barH=Math.max(12,Math.floor((H-60)/n)-4);
  const leftPad=130,rightPad=90,topPad=30,gap=4;

  ctx.clearRect(0,0,W,H);
  ctx.fillStyle="#1e293b"; ctx.fillRect(0,0,W,H);

  // X-axis label
  ctx.fillStyle="#64748b";ctx.font="11px sans-serif";ctx.textAlign="center";
  ctx.fillText(xLabel,W/2,H-8);

  const chartW=W-leftPad-rightPad;

  labels.forEach((lbl,i)=>{
    const y=topPad+i*(barH+gap);
    const bw=Math.max(2,(values[i]/maxV)*chartW);

    // bar background
    ctx.fillStyle="#263347";ctx.fillRect(leftPad,y,chartW,barH);
    // bar fill with gradient
    const g=ctx.createLinearGradient(leftPad,0,leftPad+bw,0);
    g.addColorStop(0,color);g.addColorStop(1,color+"99");
    ctx.fillStyle=g;ctx.fillRect(leftPad,y,bw,barH);

    // label
    ctx.fillStyle="#94a3b8";ctx.font="11px sans-serif";ctx.textAlign="right";
    ctx.fillText(lbl.length>16?lbl.slice(0,16)+"…":lbl,leftPad-5,y+barH/2+4);

    // value
    ctx.fillStyle="#e2e8f0";ctx.textAlign="left";
    const vStr=values[i]>=1e6?(values[i]/1e6).toFixed(1)+"M":values[i]>=1e3?(values[i]/1e3).toFixed(0)+"K":values[i];
    ctx.fillText(vStr,leftPad+bw+5,y+barH/2+4);
  });
}

// Donut chart
function drawDonut(canvas, closed, open) {
  const ctx=canvas.getContext("2d");
  const W=canvas.width, H=canvas.height;
  const cx=W/2, cy=H/2-20;
  const R=Math.min(W,H-60)/2-10;
  const inner=R*0.58;
  const total=closed+open;
  if(!total)return;
  const closedAng=(closed/total)*2*Math.PI;

  ctx.clearRect(0,0,W,H);
  ctx.fillStyle="#1e293b";ctx.fillRect(0,0,W,H);

  // closed (green)
  ctx.beginPath();ctx.moveTo(cx,cy);
  ctx.arc(cx,cy,R,-Math.PI/2,-Math.PI/2+closedAng);
  ctx.closePath();ctx.fillStyle="#22c55e";ctx.fill();

  // open (amber)
  ctx.beginPath();ctx.moveTo(cx,cy);
  ctx.arc(cx,cy,R,-Math.PI/2+closedAng,-Math.PI/2+2*Math.PI);
  ctx.closePath();ctx.fillStyle="#f59e0b";ctx.fill();

  // gap lines
  ctx.strokeStyle="#1e293b";ctx.lineWidth=2;
  ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,R,-Math.PI/2,-Math.PI/2+0.01);ctx.stroke();
  ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,R,-Math.PI/2+closedAng,-Math.PI/2+closedAng+0.01);ctx.stroke();

  // inner circle
  ctx.beginPath();ctx.arc(cx,cy,inner,0,2*Math.PI);
  ctx.fillStyle="#1e293b";ctx.fill();

  // center text
  ctx.fillStyle="#f1f5f9";ctx.font="bold 22px sans-serif";ctx.textAlign="center";
  ctx.fillText(total.toLocaleString(),cx,cy+6);
  ctx.fillStyle="#64748b";ctx.font="11px sans-serif";
  ctx.fillText("Total Items",cx,cy+22);

  // legend
  const lx=20,ly=H-40;
  ctx.fillStyle="#22c55e";ctx.fillRect(lx,ly,14,14);
  ctx.fillStyle="#94a3b8";ctx.font="12px sans-serif";ctx.textAlign="left";
  ctx.fillText(`Closed: ${closed.toLocaleString()} (${(closed/total*100).toFixed(1)}%)`,lx+18,ly+11);
  ctx.fillStyle="#f59e0b";ctx.fillRect(lx+W/2-30,ly,14,14);
  ctx.fillStyle="#94a3b8";
  ctx.fillText(`Open: ${open.toLocaleString()} (${(open/total*100).toFixed(1)}%)`,lx+W/2-12,ly+11);
}

// Line / area chart
function drawLine(canvas, labels, values, title, color) {
  const ctx=canvas.getContext("2d");
  const W=canvas.width, H=canvas.height;
  const maxV=Math.max(...values)||1;
  const lpad=50,rpad=20,tpad=30,bpad=40;
  const chartW=W-lpad-rpad, chartH=H-tpad-bpad;
  const n=labels.length;

  ctx.clearRect(0,0,W,H);
  ctx.fillStyle="#1e293b";ctx.fillRect(0,0,W,H);

  // title
  ctx.fillStyle="#94a3b8";ctx.font="11px sans-serif";ctx.textAlign="center";
  ctx.fillText(title,W/2,16);

  // grid lines (5)
  for(let i=0;i<=4;i++){
    const y=tpad+chartH*(1-i/4);
    ctx.strokeStyle="#263347";ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(lpad,y);ctx.lineTo(lpad+chartW,y);ctx.stroke();
    ctx.fillStyle="#475569";ctx.font="10px sans-serif";ctx.textAlign="right";
    const v=Math.round(maxV*i/4);
    ctx.fillText(v>=1000?Math.round(v/1000)+"K":v,lpad-4,y+3);
  }

  // area fill
  const path=new Path2D();
  path.moveTo(lpad,tpad+chartH);
  values.forEach((v,i)=>{
    const x=lpad+(i/(n-1||1))*chartW;
    const y=tpad+chartH*(1-v/maxV);
    if(i===0)path.lineTo(x,y);else path.lineTo(x,y);
  });
  path.lineTo(lpad+chartW,tpad+chartH);path.closePath();
  const g=ctx.createLinearGradient(0,tpad,0,tpad+chartH);
  g.addColorStop(0,color+"55");g.addColorStop(1,color+"00");
  ctx.fillStyle=g;ctx.fill(path);

  // line
  ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();
  values.forEach((v,i)=>{
    const x=lpad+(i/(n-1||1))*chartW;
    const y=tpad+chartH*(1-v/maxV);
    i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
  });
  ctx.stroke();

  // x labels (skip to avoid crowding)
  const step=Math.ceil(n/8);
  ctx.fillStyle="#475569";ctx.font="10px sans-serif";ctx.textAlign="center";
  labels.forEach((l,i)=>{
    if(i%step===0){
      const x=lpad+(i/(n-1||1))*chartW;
      ctx.fillText(l,x,tpad+chartH+15);
    }
  });
}
</script>
</body>
</html>
"""

# ══════════════════════════════════════════════════════════════════════════════
# 7  Inject data & write
# ══════════════════════════════════════════════════════════════════════════════
J = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

html = (HTML
        .replace("__MFRS__",     J(mfrs))
        .replace("__COMPANIES__", J(company_list))
        .replace("__PROJS__",    J(projs))
        .replace("__PGS__",      J(pgroups))
        .replace("__DATA__",     J(records))
        .replace("__STATS__",    J(stats))
        .replace("__PROD__",     J(product_cache)))

out = Path("procurement_dashboard.html")
out.write_text(html, encoding="utf-8")
sz = out.stat().st_size
print(f"Done: {out}  ({sz/1024/1024:.1f} MB, {len(records):,} rows, {len(mfrs)} manufacturers)")
print(f"      Orders: {stats['total_orders']:,}  Open: {stats['open_count']:,}  "
      f"Closed: {stats['closed_count']:,}")
print(f"      CNY: {stats['total_cny']:,.0f}  EUR: {stats['total_eur']:,.0f}")
