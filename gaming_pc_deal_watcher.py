#!/usr/bin/env python3
"""
Gaming PC Deal Watcher
Finds prebuilt gaming desktops with a GPU at RTX 5070 level or better and
alerts you (Discord and/or phone push) with a 1-10 gaming score and an
estimate of what the PC should cost.

GPUs covered (all roughly RTX 5070 performance or higher)
  NVIDIA: RTX 5070, 5070 Ti, 5080, 5090, 4070 Ti, 4070 Ti Super, 4080,
          4080 Super, 4090
  AMD:    RX 9070, 9070 XT, 7900 XT, 7900 XTX
Edit the GPU_INFO table below to add or remove cards.

Sources
  1. Best Buy official Products API (free key: https://developer.bestbuy.com)
     - requires the PC to be orderable with home delivery; nearby pickup is a bonus line
  2. Slickdeals RSS searches (covers Amazon, Newegg, Walmart, Micro Center,
     Dell, HP, Costco, and more). Only posts from the last 24 hours are kept,
     using the post's own timestamp.

Every listing alerts ONCE. Seen listings are remembered in seen_deals.json.

Usage
  python gaming_pc_deal_watcher.py            # runs forever
  python gaming_pc_deal_watcher.py --once     # single check, then exit
  python gaming_pc_deal_watcher.py --test     # send a sample alert

Settings (environment variables, all optional)
  MAX_PRICE=1500          price cap
  MAX_AGE_HOURS=24        Slickdeals posts must be newer than this
  CHECK_MINUTES=15        only used when running forever on your own computer
  PICKUP_ZIP=21501        Best Buy: also list nearby stores that have it for pickup (optional bonus)
  PICKUP_MILES=50         pickup search radius
  BESTBUY_API_KEY=...  NTFY_TOPIC=...  DISCORD_WEBHOOK=...
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

MAX_PRICE = float(os.getenv("MAX_PRICE", "1500"))
CHECK_MINUTES = float(os.getenv("CHECK_MINUTES", "15"))
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "24"))
PICKUP_ZIP = os.getenv("PICKUP_ZIP", "").strip()
PICKUP_MILES = os.getenv("PICKUP_MILES", "50").strip() or "50"
BESTBUY_API_KEY = os.getenv("BESTBUY_API_KEY", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
STATE_FILE = Path(__file__).with_name("seen_deals.json")
STATE_KEEP_DAYS = 45
UA = {"User-Agent": "Mozilla/5.0 (compatible; GamingPCDealWatcher/1.0)"}

# ------------------------------------------------------------------ GPUs
# name: (regex, gaming score 1-10, est. street price $, recommended PSU watts)
# Listed most-specific first so "5070 Ti" is checked before "5070".
# Rough estimates, NOT live prices. Edit freely.
GPU_INFO = [
    ("RTX 5090",          r"RTX\s*-?\s*5090(?!\d)",                       10.0, 2300, 1000),
    ("RTX 4090",          r"RTX\s*-?\s*4090(?!\d)",                        9.8, 1800, 850),
    ("RTX 5080",          r"RTX\s*-?\s*5080(?!\d)",                        9.4, 1050, 850),
    ("RTX 5070 Ti",       r"RTX\s*-?\s*5070\s*-?\s*Ti(?![a-z])",           8.7,  780, 750),
    ("RTX 4080 Super",    r"RTX\s*-?\s*4080\s*-?\s*Super",                 8.6,  950, 750),
    ("RTX 4080",          r"RTX\s*-?\s*4080(?!\d)",                        8.5,  900, 750),
    ("RX 7900 XTX",       r"RX\s*-?\s*7900\s*-?\s*XTX",                    8.6,  800, 800),
    ("RX 9070 XT",        r"RX\s*-?\s*9070\s*-?\s*XT",                     8.5,  650, 750),
    ("RTX 4070 Ti Super", r"RTX\s*-?\s*4070\s*-?\s*Ti\s*-?\s*Super",       8.2,  750, 700),
    ("RX 9070",           r"RX\s*-?\s*9070(?!\d)(?!\s*-?\s*(?:XT|GRE))",   8.0,  560, 650),
    ("RX 7900 XT",        r"RX\s*-?\s*7900\s*-?\s*XT(?![A-Za-z])",         7.9,  650, 750),
    ("RTX 5070",          r"RTX\s*-?\s*5070(?!\d)",                        7.8,  550, 750),
    ("RTX 4070 Ti",       r"RTX\s*-?\s*4070\s*-?\s*Ti(?![a-z])",           7.7,  600, 700),
]
GPU_TABLE = {n: (re.compile(rx, re.I), sc, pr, w) for n, rx, sc, pr, w in GPU_INFO}

# Things that are NOT a desktop PC
EXCLUDE_RE = re.compile(
    r"laptop|notebook|\bmobile\b|monitor|motherboard|graphics card|video card|"
    r"\bgpu only\b|handheld|\bmini ?pc\b.*\boculink|\bcase\b only",
    re.I,
)
DESKTOP_RE = re.compile(r"desktop|gaming pc|\bpc\b|tower|prebuilt|pre-built|\bomen\b|alienware|aurora", re.I)
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)")
INSTORE_ONLY_RE = re.compile(
    r"in[- ]store only|store pickup only|pickup only|in[- ]store pickup only|in[- ]store exclusive", re.I)
FREE_SHIP_RE = re.compile(r"free (?:shipping|s/h|delivery)|ships free", re.I)
EXPIRED_RE = re.compile(r"this deal has expired|deal has expired|expired deal", re.I)

SLICKDEALS_QUERIES = [
    "rtx 5070 desktop", "rtx 5070 ti desktop", "rtx 5080 desktop", "rtx 5090 desktop",
    "rtx 5070 gaming pc", "rtx 4080 desktop", "rtx 4070 ti desktop", "rtx 4090 desktop",
    "radeon rx 9070 desktop", "radeon rx 7900 desktop", "rx 9070 xt gaming pc",
]
# (brand word, model number) pairs sent to the Best Buy search
BESTBUY_SEARCHES = [
    ("rtx", "5070"), ("rtx", "5080"), ("rtx", "5090"),
    ("rtx", "4070"), ("rtx", "4080"), ("rtx", "4090"),
    ("radeon", "9070"), ("radeon", "7900"),
]


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_gpu(t):
    for name, (rx, *_rest) in GPU_TABLE.items():
        if rx.search(t):
            return name
    return None


def looks_like_match(title):
    return bool(parse_gpu(title)) and not EXCLUDE_RE.search(title) and bool(DESKTOP_RE.search(title))


def first_price(text):
    """Best guess at the actual selling price in a deal title.
    Skips discount amounts ('$200 off', 'save $300', 'w/ $100 gift card'),
    then takes the lowest remaining price, so '$1,299 (was $1,899)' reads as 1299.
    Returns None when the title only shows discounts (no final price)."""
    prices = []
    for m in PRICE_RE.finditer(text):
        after = text[m.end():m.end() + 14].lower()
        before = text[max(0, m.start() - 14):m.start()].lower()
        if re.match(r"\s*(\+\s*)?(off|savings|rebate|gift|credit|back|cash ?back|discount|coupon)", after):
            continue
        if re.search(r"(save|saving|savings|extra|up to|take|get|minus|w/|with|off)\s*$", before):
            continue
        val = float(m.group(1).replace(",", ""))
        if val >= 400:
            prices.append(val)
    return min(prices) if prices else None


def age_text(posted):
    if not posted:
        return ""
    mins = int((datetime.now(timezone.utc) - posted).total_seconds() // 60)
    return f"posted {mins} min ago" if mins < 90 else f"posted {mins // 60}h ago"


# ---------------------------------------------------------------- Best Buy
def fetch_bestbuy():
    if not BESTBUY_API_KEY:
        return []
    deals = []
    for brand, model in BESTBUY_SEARCHES:
        q = f"(search=desktop&search={brand}&search={model}&salePrice<{MAX_PRICE:.0f}&onlineAvailability=true)"
        url = (
            "https://api.bestbuy.com/v1/products"
            + urllib.parse.quote(q, safe="=&()<>")
            + "?"
            + urllib.parse.urlencode(
                {"apiKey": BESTBUY_API_KEY, "format": "json",
                 "show": "sku,name,salePrice,regularPrice,url,homeDelivery,orderable,freeShipping,shippingCost",
                 "pageSize": 50}
            )
        )
        try:
            data = json.loads(http_get(url))
        except Exception as e:
            print(f"[Best Buy] {brand} {model} lookup failed: {e}")
            continue
        for p in data.get("products", []):
            # Shipping is the priority: skip anything Best Buy says can't be
            # delivered or isn't currently orderable.
            if p.get("homeDelivery") is False:
                continue
            if p.get("orderable") not in (None, "Available"):
                continue
            if p.get("freeShipping"):
                ship = "🚚 Ships to you - FREE shipping"
            elif p.get("shippingCost") is not None:
                ship = f"🚚 Ships to you - shipping ${float(p['shippingCost']):,.0f}"
            else:
                ship = "🚚 Ships to you"
            deals.append(
                {"id": f"bby-{p['sku']}", "sku": p["sku"], "source": "Best Buy",
                 "title": p["name"], "price": p["salePrice"], "was": p.get("regularPrice"),
                 "url": p["url"], "posted": None, "ship": ship, "rank": 0}
            )
        time.sleep(0.3)
    return deals


def bestbuy_pickup_stores(sku):
    """Stores near PICKUP_ZIP with this SKU in stock for pickup (None if lookup failed)."""
    q = f"stores(area({PICKUP_ZIP},{PICKUP_MILES}))+products(sku={sku})"
    url = (
        "https://api.bestbuy.com/v1/"
        + urllib.parse.quote(q, safe="=&(),+")
        + "?"
        + urllib.parse.urlencode(
            {"apiKey": BESTBUY_API_KEY, "format": "json",
             "show": "storeId,name,city,state,distance", "pageSize": 5}
        )
    )
    try:
        data = json.loads(http_get(url))
    except Exception as e:
        print(f"[Best Buy] pickup check failed for {sku}: {e}")
        return None
    out = []
    for s in data.get("stores", []):
        dist = f"{s['distance']:.0f} mi" if s.get("distance") is not None else ""
        out.append(f"{s.get('name', 'Best Buy')} ({s.get('city', '')}{', ' + dist if dist else ''})")
    return out


# -------------------------------------------------------------- Slickdeals
def fetch_slickdeals():
    deals = []
    now = datetime.now(timezone.utc)
    for q in SLICKDEALS_QUERIES:
        url = "https://slickdeals.net/newsearch.php?" + urllib.parse.urlencode(
            {"q": q, "searchin": "first", "rss": "1"}
        )
        try:
            root = ET.fromstring(http_get(url))
        except Exception as e:
            print(f"[Slickdeals] '{q}' failed: {e}")
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            price = first_price(title)
            if price is None:
                continue
            # Post timestamp must exist and be within the last MAX_AGE_HOURS
            try:
                posted = parsedate_to_datetime(item.findtext("pubDate") or "")
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if not (0 <= (now - posted).total_seconds() <= MAX_AGE_HOURS * 3600):
                continue
            if INSTORE_ONLY_RE.search(title):
                continue  # can't be shipped
            m = re.search(r"/f/(\d+)", link)
            deal_id = "sd-" + (m.group(1) if m else (item.findtext("guid") or link))
            deals.append(
                {"id": deal_id, "source": "Slickdeals", "title": title, "price": price,
                 "was": None, "url": link, "posted": posted,
                 "ship": ("🚚 Free shipping (per post title)" if FREE_SHIP_RE.search(title)
                          else "🚚 Shipping not confirmed - check the page"),
                 "rank": 1 if FREE_SHIP_RE.search(title) else 2}
            )
        time.sleep(0.5)
    return deals


def slickdeals_expired(url):
    """Best effort: True if the deal page says the deal has expired."""
    try:
        page = http_get(url, timeout=15).decode("utf-8", "ignore")
    except Exception:
        return False  # can't tell, so keep the deal
    return bool(EXPIRED_RE.search(page))


# ---------------------------------------------------------------- rating
# Rough street-price estimates used to guess what a PC "should" cost.
# NOT live prices. RAM/GPU prices move a lot, so tweak when they drift.
RAM_PER_GB = {"ddr5": 5.0, "ddr4": 3.0, "unknown": 4.5}   # $ per GB
SSD_PER_TB = 65.0                                          # $ per TB
PLATFORM_COST = 320     # case + motherboard + PSU + cooler + Windows
BUILDER_MARKUP = 1.08   # normal prebuilt assembly/warranty markup
RAM_SCORES = {8: 2.5, 16: 5.5, 24: 6.5, 32: 8.0, 48: 9.0, 64: 9.7, 96: 10, 128: 10}
WEIGHTS = {"gpu": 0.50, "cpu": 0.20, "ram": 0.15, "ssd": 0.10, "psu": 0.05}


def parse_cpu(t):
    """Returns (label, score, est_price) or None."""
    m = re.search(r"Core\s*Ultra\s*([579])\s*(\d{3})\s*(KF|K|F)?", t, re.I)
    if m:
        tier = int(m.group(1))
        return (f"Core Ultra {tier} {m.group(2)}{(m.group(3) or '').upper()}",
                {5: 6.8, 7: 7.6, 9: 8.2}[tier], {5: 240, 7: 340, 9: 540}[tier])
    m = re.search(r"Ryzen\s*([3579])\s*(?:PRO\s*)?(\d{4,5})\s*(X3D|XT|X|G|F)?", t, re.I)
    if m:
        tier, model, suf = int(m.group(1)), m.group(2), (m.group(3) or "").upper()
        gen = int(model[0])
        score = {3: 5.0, 5: 6.5, 7: 7.5, 9: 8.2}[tier]
        score += {9: 1.0, 8: 0.7, 7: 0.4, 5: -0.4, 4: -0.4, 3: -1.0}.get(gen, 0)
        price = {3: 110, 5: 190, 7: 300, 9: 470}[tier]
        if suf == "X3D":
            score, price = score + 1.8, price + 110
        if suf == "G":
            score -= 0.8
        return (f"Ryzen {tier} {model}{suf}", max(1.0, min(10.0, score)), price)
    m = re.search(r"\bi([3579])[- ]?(\d{4,5})\s*(KF|KS|K|F|T)?", t, re.I)
    if m:
        tier, model, suf = int(m.group(1)), m.group(2), (m.group(3) or "").upper()
        gen = int(model[:2]) if len(model) == 5 else int(model[0])
        score = {3: 5.0, 5: 6.5, 7: 7.4, 9: 8.0}[tier]
        score += {14: 0.5, 13: 0.3, 12: 0.0, 11: -0.8, 10: -1.2}.get(gen, -1.5)
        price = {3: 120, 5: 200, 7: 320, 9: 480}[tier]
        return (f"Core i{tier}-{model}{suf}", max(1.0, min(10.0, score)), price)
    return None


def parse_ram(t):
    """Returns (gb, ddr_type) or None. GPU names + VRAM are stripped first."""
    s = re.sub(r"(?:RTX|RX)\s*-?\s*\d{4}(?:\s*-?\s*(?:Ti|XTX|XT|Super))*(?:\s*\d{1,2}\s*GB)?", " ", t, flags=re.I)
    for m in re.finditer(r"(?<![\d.])(\d{1,3})\s?GB\b(?!\s*(?:SSD|NVMe|storage|VRAM|GDDR))", s, re.I):
        gb = int(m.group(1))
        if gb in RAM_SCORES:
            ddr = "ddr5" if re.search(r"DDR5", t, re.I) else "ddr4" if re.search(r"DDR4", t, re.I) else "unknown"
            return gb, ddr
    return None


def parse_storage_tb(t):
    total = 0.0
    for m in re.finditer(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s?(TB|GB)\b(?:\s*(?:Gen\d\s*)?(PCIe|NVMe|SSD|M\.2|Solid|Storage|HDD|HD\b))?",
        t, re.I,
    ):
        val, unit, kind = float(m.group(1)), m.group(2).upper(), (m.group(3) or "").upper()
        if kind.startswith("HD"):
            continue
        if unit == "TB" and val <= 16:
            total += val
        elif unit == "GB" and kind and val >= 240:
            total += val / 1000
    return total or None


def parse_psu_watts(t):
    ws = [int(m.group(1)) for m in re.finditer(r"(?<![\d.])(\d{3,4})\s?W(?:att)?\b", t)]
    ws = [w for w in ws if 450 <= w <= 1600]
    return max(ws) if ws else None


def ssd_score(tb):
    for limit, sc in ((0.5, 3.5), (1.0, 7.0), (2.0, 9.0), (4.0, 9.7)):
        if tb <= limit:
            return sc
    return 10.0


def rate_pc(title, price):
    """Gaming score (1-10) from specs in the title, plus an estimate of fair price."""
    gpu = parse_gpu(title)
    if not gpu:
        return None
    _, gpu_score, gpu_price, rec_watts = GPU_TABLE[gpu]
    cpu, ram, tb, watts = parse_cpu(title), parse_ram(title), parse_storage_tb(title), parse_psu_watts(title)

    found, missing = [gpu], []
    if cpu:
        cpu_label, cpu_score, cpu_price = cpu
        found.append(cpu_label)
    else:
        cpu_label, cpu_score, cpu_price = None, 6.0, 200      # assume weak mid-range
        missing.append("CPU")
    if ram:
        gb, ddr = ram
        ram_score, ram_price = RAM_SCORES[gb], gb * RAM_PER_GB[ddr]
        found.append(f"{gb}GB RAM")
    else:
        gb = None
        ram_score, ram_price = RAM_SCORES[16], 16 * RAM_PER_GB["unknown"]   # assume 16GB
        missing.append("RAM")
    if tb:
        ssd_sc, ssd_price = ssd_score(tb), tb * SSD_PER_TB
        found.append(f"{tb:g}TB storage")
    else:
        ssd_sc, ssd_price = 4.5, 0.5 * SSD_PER_TB             # assume 512GB
        missing.append("storage")

    parts = {"gpu": gpu_score, "cpu": cpu_score, "ram": ram_score, "ssd": ssd_sc}
    weights = dict(WEIGHTS)
    psu_note = ""
    if watts:
        parts["psu"] = 10.0 if watts >= rec_watts * 1.2 else 8.5 if watts >= rec_watts else max(1.0, 5.0 * watts / rec_watts)
        found.append(f"{watts}W PSU")
        if watts < rec_watts:
            psu_note = f"PSU ({watts}W) is below the ~{rec_watts}W recommended for this GPU"
    else:
        weights.pop("psu")
    score = sum(parts[k] * weights[k] for k in parts) / sum(weights[k] for k in parts)
    score = round(max(1.0, min(10.0, score)), 1)

    fair = (gpu_price + cpu_price + ram_price + ssd_price + PLATFORM_COST) * BUILDER_MARKUP
    fair = round(fair / 10) * 10
    # Fingerprint lets us spot the same PC listed on two sites at the same price
    fp = f"fp-{gpu}|{cpu_label}|{gb}|{tb}|{int(price)}" if (cpu_label and gb) else None
    return {"score": score, "parts": parts, "fair": fair, "ratio": price / fair,
            "found": found, "missing": missing, "psu_note": psu_note, "fp": fp}


def verdict(ratio):
    if ratio <= 0.80:
        return "🔥 EXCELLENT DEAL"
    if ratio <= 0.92:
        return "✅ GREAT DEAL"
    if ratio <= 1.03:
        return "👍 FAIR PRICE"
    if ratio <= 1.15:
        return "😐 A BIT PRICEY"
    return "❌ OVERPRICED"


def rating_lines(r, price):
    if not r:
        return []
    p = r["parts"]
    lines = [f"**Gaming score: {r['score']}/10**  (GPU {p['gpu']:.1f} · CPU {p['cpu']:.1f} · "
             f"RAM {p['ram']:.1f} · Storage {p['ssd']:.1f})"]
    diff = r["fair"] - price
    cmp_txt = (f"deal is ~${diff:,.0f} ({diff / r['fair']:.0%}) below that" if diff >= 0
               else f"deal is ~${-diff:,.0f} ({-diff / r['fair']:.0%}) above that")
    lines.append(f"**Should cost about ${r['fair']:,.0f}** - {cmp_txt}. {verdict(r['ratio'])}")
    lines.append("Specs read from title: " + ", ".join(r["found"]))
    if r["missing"]:
        lines.append("Not listed: " + ", ".join(r["missing"]) +
                     " (assumed low-end, so score/price are rough - check the page)")
    if r["psu_note"]:
        lines.append("⚠️ " + r["psu_note"])
    lines.append("(Fair price is an estimate from typical part prices, not a live quote.)")
    return lines


# ------------------------------------------------------------------- core
def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return {}
    now = time.time()
    for v in state.values():
        v.setdefault("t", now)
    return {k: v for k, v in state.items() if now - v["t"] < STATE_KEEP_DAYS * 86400}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def notify(deal):
    was = f" (was ${deal['was']:,.0f})" if deal.get("was") else ""
    lines = [f"[{deal['source']}] ${deal['price']:,.0f}{was}", deal["title"]]
    if deal.get("ship"):
        lines.append(deal["ship"])
    if deal.get("stores"):
        lines.append("🏬 Bonus - also in stock for pickup: " + "; ".join(deal["stores"]))
    if deal.get("posted"):
        lines.append(age_text(deal["posted"]) + " - may sell out or end soon")
    lines += rating_lines(deal.get("rating"), deal["price"])
    if deal.get("note"):
        lines.append(deal["note"])
    lines.append(deal["url"])
    msg = "\n".join(lines)[:1900]
    print("\n🎮 NEW DEAL\n" + msg + "\n")
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                headers={"Title": "Gaming PC deal", "Click": deal["url"], **UA})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"[ntfy] failed: {e}")
    if DISCORD_WEBHOOK:
        try:
            req = urllib.request.Request(
                DISCORD_WEBHOOK, data=json.dumps({"content": msg}).encode("utf-8"),
                headers={"Content-Type": "application/json", **UA})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"[Discord] failed: {e}")


def check_once():
    state = load_state()
    candidates = fetch_bestbuy() + fetch_slickdeals()
    candidates.sort(key=lambda d: d.get("rank", 2))  # confirmed shipping first
    seen_this_run, new = set(), 0
    for d in candidates:
        if d["id"] in seen_this_run:
            continue
        seen_this_run.add(d["id"])
        if not looks_like_match(d["title"]) or d["price"] > MAX_PRICE or d["price"] < 400:
            continue
        if d["id"] in state:
            continue  # already alerted once - never again

        d["rating"] = rate_pc(d["title"], d["price"])
        fp = d["rating"]["fp"] if d["rating"] else None
        entry = {"price": d["price"], "title": d["title"], "t": time.time()}
        if fp and fp in state:  # same PC at same price already sent from another listing
            state[d["id"]] = entry
            continue

        if d["source"] == "Best Buy" and PICKUP_ZIP:
            # Pickup is only a bonus line; it never blocks an alert
            stores = bestbuy_pickup_stores(d["sku"])
            time.sleep(0.3)
            if stores:
                d["stores"] = stores
        elif d["source"] == "Slickdeals" and slickdeals_expired(d["url"]):
            state[d["id"]] = entry
            continue

        notify(d)
        state[d["id"]] = entry
        if fp:
            state[fp] = entry
        save_state(state)  # save right away so a crash can't cause repeats
        new += 1
    save_state(state)
    print(f"{time.strftime('%H:%M:%S')} checked {len(seen_this_run)} listings, {new} new deal(s).")


def main():
    if "--test" in sys.argv:
        t = "HP OMEN 35L Gaming Desktop - AMD Ryzen 7 9700X - 32GB DDR5 - NVIDIA GeForce RTX 5070 - 1TB SSD"
        notify({"source": "Test (sample alert)", "title": t, "price": 1299, "was": 1599,
                "url": "https://example.com", "rating": rate_pc(t, 1299)})
        return
    if "--once" in sys.argv:
        check_once()
        return
    print(f"Watching for RTX 5070-class gaming desktops <= ${MAX_PRICE:,.0f} every {CHECK_MINUTES:g} min. Ctrl+C to stop.")
    if not BESTBUY_API_KEY:
        print("Tip: set BESTBUY_API_KEY to also query Best Buy directly.")
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"Check failed: {e}")
        time.sleep(CHECK_MINUTES * 60)


if __name__ == "__main__":
    main()
