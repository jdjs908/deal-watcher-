#!/usr/bin/env python3
"""
Gaming PC Deal Watcher
Looks for prebuilt gaming desktops with an NVIDIA RTX 5070 or better
(5070, 5070 Ti, 5080, 5090) at or under your price cap, and alerts you.

Sources
  1. Best Buy official Products API (free key: https://developer.bestbuy.com)
     - optionally requires the PC to be in stock for pickup near your ZIP
  2. Slickdeals RSS searches (covers Amazon, Newegg, Walmart, Micro Center,
     Dell, HP, Costco, and more). Only recent posts are kept, and posts that
     the Slickdeals page marks as expired are dropped.

Alerts: console, plus optional ntfy.sh phone push and/or Discord webhook.

Usage
  python gaming_pc_deal_watcher.py            # runs forever
  python gaming_pc_deal_watcher.py --once     # single check, then exit
  python gaming_pc_deal_watcher.py --test     # send a test alert

Settings (environment variables, all optional)
  MAX_PRICE=1500          price cap
  CHECK_MINUTES=15        only used when running forever on your own computer
  MAX_AGE_HOURS=12        ignore Slickdeals posts older than this
  PICKUP_ZIP=21501        Best Buy: require in-stock pickup near this ZIP
  PICKUP_MILES=50         pickup search radius
  BESTBUY_API_KEY=...
  NTFY_TOPIC=...
  DISCORD_WEBHOOK=...
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
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "12"))
PICKUP_ZIP = os.getenv("PICKUP_ZIP", "").strip()
PICKUP_MILES = os.getenv("PICKUP_MILES", "50").strip() or "50"
BESTBUY_API_KEY = os.getenv("BESTBUY_API_KEY", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
STATE_FILE = Path(__file__).with_name("seen_deals.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; GamingPCDealWatcher/1.0)"}

# RTX 5070 or better (desktop prebuilts). 5070 Ti / 5080 / 5090 all match.
GPU_RE = re.compile(r"RTX\s*-?\s*50(70|80|90)\b", re.I)
# Things that are NOT a desktop PC
EXCLUDE_RE = re.compile(
    r"laptop|notebook|\bmobile\b|monitor|motherboard|graphics card|video card|"
    r"\bgpu only\b|handheld|\bmini ?pc\b.*\boculink|\bcase\b only",
    re.I,
)
DESKTOP_RE = re.compile(r"desktop|gaming pc|\bpc\b|tower|prebuilt|pre-built|\bomen\b|alienware|aurora", re.I)
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)")
EXPIRED_RE = re.compile(r"this deal has expired|deal has expired|expired deal", re.I)

SLICKDEALS_QUERIES = [
    "rtx 5070 desktop",
    "rtx 5070 ti desktop",
    "rtx 5080 desktop",
    "rtx 5090 desktop",
    "rtx 5070 gaming pc",
]


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def looks_like_match(title):
    return bool(GPU_RE.search(title)) and not EXCLUDE_RE.search(title) and bool(DESKTOP_RE.search(title))


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
    for model in ("5070", "5080", "5090"):
        q = f"(search=desktop&search=rtx&search={model}&salePrice<{MAX_PRICE:.0f}&onlineAvailability=true)"
        url = (
            "https://api.bestbuy.com/v1/products"
            + urllib.parse.quote(q, safe="=&()<>")
            + "?"
            + urllib.parse.urlencode(
                {
                    "apiKey": BESTBUY_API_KEY,
                    "format": "json",
                    "show": "sku,name,salePrice,regularPrice,url",
                    "pageSize": 50,
                }
            )
        )
        try:
            data = json.loads(http_get(url))
        except Exception as e:
            print(f"[Best Buy] {model} lookup failed: {e}")
            continue
        for p in data.get("products", []):
            deals.append(
                {
                    "id": f"bby-{p['sku']}",
                    "sku": p["sku"],
                    "source": "Best Buy",
                    "title": p["name"],
                    "price": p["salePrice"],
                    "was": p.get("regularPrice"),
                    "url": p["url"],
                    "posted": None,
                }
            )
    return deals


def bestbuy_pickup_stores(sku):
    """Stores near PICKUP_ZIP that have this SKU in stock for pickup.
    Returns a list of strings, or None if the lookup failed."""
    q = f"stores(area({PICKUP_ZIP},{PICKUP_MILES}))+products(sku={sku})"
    url = (
        "https://api.bestbuy.com/v1/"
        + urllib.parse.quote(q, safe="=&(),+")
        + "?"
        + urllib.parse.urlencode(
            {
                "apiKey": BESTBUY_API_KEY,
                "format": "json",
                "show": "storeId,name,city,state,distance",
                "pageSize": 5,
            }
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
        url = (
            "https://slickdeals.net/newsearch.php?"
            + urllib.parse.urlencode({"q": q, "searchin": "first", "rss": "1"})
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
            posted = None
            try:
                posted = parsedate_to_datetime(item.findtext("pubDate") or "")
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
            except Exception:
                pass
            # Drop old posts: their sale has often ended
            if posted and (now - posted).total_seconds() > MAX_AGE_HOURS * 3600:
                continue
            deals.append(
                {
                    "id": "sd-" + (item.findtext("guid") or link),
                    "source": "Slickdeals",
                    "title": title,
                    "price": price,
                    "was": None,
                    "url": link,
                    "posted": posted,
                }
            )
    return deals


def slickdeals_expired(url):
    """Best effort: True if the deal page says the deal has expired."""
    try:
        page = http_get(url, timeout=15).decode("utf-8", "ignore")
    except Exception:
        return False  # can't tell, so don't throw the deal away
    return bool(EXPIRED_RE.search(page))



# ---------------------------------------------------------------- rating
# Rough street-price estimates used to guess what a PC "should" cost.
# These are NOT live prices. RAM/GPU prices move a lot, so tweak these
# numbers whenever you notice the estimates drifting from reality.
GPU_INFO = {            # name: (gaming score 1-10, est. price $)
    "5070": (7.8, 550),
    "5070 Ti": (8.7, 780),
    "5080": (9.4, 1050),
    "5090": (10.0, 2300),
}
GPU_PSU_WATTS = {"5070": 750, "5070 Ti": 750, "5080": 850, "5090": 1000}
RAM_PER_GB = {"ddr5": 5.0, "ddr4": 3.0, "unknown": 4.5}   # $ per GB
SSD_PER_TB = 65.0                                          # $ per TB
PLATFORM_COST = 320     # case + motherboard + PSU + cooler + Windows
BUILDER_MARKUP = 1.08   # normal prebuilt assembly/warranty markup
RAM_SCORES = {8: 2.5, 16: 5.5, 24: 6.5, 32: 8.0, 48: 9.0, 64: 9.7, 96: 10, 128: 10}
WEIGHTS = {"gpu": 0.50, "cpu": 0.20, "ram": 0.15, "ssd": 0.10, "psu": 0.05}


def parse_gpu(t):
    m = GPU_RE.search(t)
    if not m:
        return None
    model = "50" + m.group(1)
    if model == "5070" and re.search(r"5070\s*-?\s*Ti", t, re.I):
        model = "5070 Ti"
    return model


def parse_cpu(t):
    """Returns (label, score, est_price) or None."""
    m = re.search(r"Core\s*Ultra\s*([579])\s*(\d{3})\s*(KF|K|F)?", t, re.I)
    if m:
        tier = int(m.group(1))
        score = {5: 6.8, 7: 7.6, 9: 8.2}[tier]
        price = {5: 240, 7: 340, 9: 540}[tier]
        return (f"Core Ultra {tier} {m.group(2)}{(m.group(3) or '').upper()}", score, price)
    m = re.search(r"Ryzen\s*([3579])\s*(?:PRO\s*)?(\d{4,5})\s*(X3D|XT|X|G|F)?", t, re.I)
    if m:
        tier, model, suf = int(m.group(1)), m.group(2), (m.group(3) or "").upper()
        gen = int(model[0])
        score = {3: 5.0, 5: 6.5, 7: 7.5, 9: 8.2}[tier]
        score += {9: 1.0, 8: 0.7, 7: 0.4, 5: -0.4, 4: -0.4, 3: -1.0}.get(gen, 0)
        price = {3: 110, 5: 190, 7: 300, 9: 470}[tier]
        if suf == "X3D":
            score += 1.8
            price += 110
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
    """Returns (gb, ddr_type) or None. Strips GPU VRAM first so '5070 Ti 16GB' isn't read as RAM."""
    s = re.sub(r"RTX\s*-?\s*50\d0(?:\s*Ti)?(?:\s*\d{1,2}\s*GB)?", " ", t, flags=re.I)
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
    gpu_score, gpu_price = GPU_INFO[gpu]
    cpu, ram, tb, watts = parse_cpu(title), parse_ram(title), parse_storage_tb(title), parse_psu_watts(title)

    found, missing = [f"RTX {gpu}"], []
    # CPU (assume a weak mid-range chip if the title doesn't say)
    if cpu:
        cpu_label, cpu_score, cpu_price = cpu
        found.append(cpu_label)
    else:
        cpu_score, cpu_price = 6.0, 200
        missing.append("CPU")
    # RAM (assume 16GB if unlisted)
    if ram:
        gb, ddr = ram
        ram_score, ram_price = RAM_SCORES[gb], gb * RAM_PER_GB[ddr]
        found.append(f"{gb}GB RAM")
    else:
        ram_score, ram_price = RAM_SCORES[16], 16 * RAM_PER_GB["unknown"]
        missing.append("RAM")
    # Storage (assume 512GB if unlisted)
    if tb:
        ssd_sc, ssd_price = ssd_score(tb), tb * SSD_PER_TB
        found.append(f"{tb:g}TB storage")
    else:
        ssd_sc, ssd_price = 4.5, 0.5 * SSD_PER_TB
        missing.append("storage")

    parts = {"gpu": gpu_score, "cpu": cpu_score, "ram": ram_score, "ssd": ssd_sc}
    weights = dict(WEIGHTS)
    psu_note = ""
    if watts:
        rec = GPU_PSU_WATTS[gpu]
        parts["psu"] = 10.0 if watts >= rec * 1.2 else 8.5 if watts >= rec else max(1.0, 5.0 * watts / rec)
        found.append(f"{watts}W PSU")
        if watts < rec:
            psu_note = f"PSU ({watts}W) is below the ~{rec}W NVIDIA recommends for this GPU"
    else:
        weights.pop("psu")
    total_w = sum(weights[k] for k in parts)
    score = sum(parts[k] * weights[k] for k in parts) / total_w
    score = round(max(1.0, min(10.0, score)), 1)

    fair = (gpu_price + cpu_price + ram_price + ssd_price + PLATFORM_COST) * BUILDER_MARKUP
    fair = round(fair / 10) * 10
    return {
        "score": score,
        "parts": parts,
        "fair": fair,
        "ratio": price / fair if fair else None,
        "found": found,
        "missing": missing,
        "psu_note": psu_note,
    }


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
    lines = [
        f"**Gaming score: {r['score']}/10**  (GPU {p['gpu']:.1f} · CPU {p['cpu']:.1f} · RAM {p['ram']:.1f} · Storage {p['ssd']:.1f})",
    ]
    diff = r["fair"] - price
    if diff >= 0:
        cmp_txt = f"deal is ~${diff:,.0f} ({diff / r['fair']:.0%}) below that"
    else:
        cmp_txt = f"deal is ~${-diff:,.0f} ({-diff / r['fair']:.0%}) above that"
    lines.append(f"**Should cost about ${r['fair']:,.0f}** - {cmp_txt}. {verdict(r['ratio'])}")
    lines.append("Specs read from title: " + ", ".join(r["found"]))
    if r["missing"]:
        lines.append(
            "Not listed: " + ", ".join(r["missing"]) + " (assumed low-end, so score/price are rough - check the page)"
        )
    if r["psu_note"]:
        lines.append("⚠️ " + r["psu_note"])
    lines.append("(Fair price is an estimate from typical part prices, not a live quote.)")
    return lines


# ------------------------------------------------------------------- core
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def notify(deal):
    was = f" (was ${deal['was']:,.0f})" if deal.get("was") else ""
    lines = [f"[{deal['source']}] ${deal['price']:,.0f}{was}", deal["title"]]
    if deal.get("posted"):
        lines.append(age_text(deal["posted"]) + " - may sell out or end soon")
    if deal.get("stores"):
        lines.append("In stock for pickup: " + "; ".join(deal["stores"]))
    lines += rating_lines(deal.get("rating"), deal["price"])
    if deal.get("note"):
        lines.append(deal["note"])
    lines.append(deal["url"])
    msg = "\n".join(lines)
    print("\n🎮 NEW DEAL\n" + msg + "\n")
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=msg.encode("utf-8"),
                headers={"Title": "Gaming PC deal", "Click": deal["url"], **UA},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"[ntfy] failed: {e}")
    if DISCORD_WEBHOOK:
        try:
            req = urllib.request.Request(
                DISCORD_WEBHOOK,
                data=json.dumps({"content": msg}).encode("utf-8"),
                headers={"Content-Type": "application/json", **UA},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"[Discord] failed: {e}")


def check_once():
    state = load_state()
    candidates = fetch_bestbuy() + fetch_slickdeals()
    new = 0
    for d in candidates:
        if not looks_like_match(d["title"]) or d["price"] > MAX_PRICE or d["price"] < 400:
            continue
        prev = state.get(d["id"])
        if prev is not None and d["price"] >= prev["price"]:
            continue  # already alerted at this price or lower

        if d["source"] == "Best Buy" and PICKUP_ZIP:
            stores = bestbuy_pickup_stores(d["sku"])
            time.sleep(0.3)
            if stores is None:
                d["note"] = "(couldn't verify pickup stock - check the page)"
            elif not stores:
                continue  # nothing in stock nearby; try again next run
            else:
                d["stores"] = stores
        elif d["source"] == "Slickdeals" and slickdeals_expired(d["url"]):
            state[d["id"]] = {"price": d["price"], "title": d["title"]}  # don't re-check
            continue

        d["rating"] = rate_pc(d["title"], d["price"])
        notify(d)
        state[d["id"]] = {"price": d["price"], "title": d["title"]}
        new += 1
    save_state(state)
    print(f"{time.strftime('%H:%M:%S')} checked {len(candidates)} listings, {new} new deal(s).")


def main():
    if "--test" in sys.argv:
        t = "HP OMEN 35L Gaming Desktop - AMD Ryzen 7 9700X - 32GB DDR5 - NVIDIA GeForce RTX 5070 - 1TB SSD"
        notify({"source": "Test (sample alert)", "title": t, "price": 1299, "was": 1599, "url": "https://example.com",
                "rating": rate_pc(t, 1299)})
        return
    if "--once" in sys.argv:
        check_once()
        return
    print(f"Watching for RTX 5070+ gaming desktops <= ${MAX_PRICE:,.0f} every {CHECK_MINUTES:g} min. Ctrl+C to stop.")
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
