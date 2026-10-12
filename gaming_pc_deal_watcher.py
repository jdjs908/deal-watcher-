#!/usr/bin/env python3
"""
Gaming PC Deal Watcher
Looks for prebuilt gaming desktops with an NVIDIA RTX 5070 or better
(5070, 5070 Ti, 5080, 5090) at or under your price cap, and alerts you.

Sources
  1. Best Buy official Products API (free key: https://developer.bestbuy.com)
  2. Slickdeals RSS searches (community-vetted deals that cover Amazon, Newegg,
     Walmart, Micro Center, Dell, HP, Costco, Best Buy, and more)

Alerts: console, plus optional ntfy.sh phone push and/or Discord webhook.

Usage
  python gaming_pc_deal_watcher.py            # runs forever, checks every 15 min
  python gaming_pc_deal_watcher.py --once     # single check, then exit

Settings (environment variables, all optional except as noted)
  MAX_PRICE=1500
  CHECK_MINUTES=15
  BESTBUY_API_KEY=...        (enables the Best Buy source)
  NTFY_TOPIC=my-secret-topic (install the ntfy app, subscribe to the same topic)
  DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_PRICE = float(os.getenv("MAX_PRICE", "1500"))
CHECK_MINUTES = float(os.getenv("CHECK_MINUTES", "15"))
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
PRICE_RE = re.compile(r"\$\s?([\d]{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")

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
    Ignores '$200 off' style amounts and tiny numbers, then takes the lowest
    remaining price (so '$1,299 (was $1,899)' reads as 1299)."""
    prices = []
    for m in PRICE_RE.finditer(text):
        after = text[m.end():m.end() + 12].lower()
        if re.match(r"\s*(off|savings|rebate|gift|credit|back|cash ?back)", after):
            continue
        val = float(m.group(1).replace(",", ""))
        if val >= 400:
            prices.append(val)
    return min(prices) if prices else None


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
                    "source": "Best Buy",
                    "title": p["name"],
                    "price": p["salePrice"],
                    "was": p.get("regularPrice"),
                    "url": p["url"],
                }
            )
    return deals


def fetch_slickdeals():
    deals = []
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
            deals.append(
                {
                    "id": "sd-" + (item.findtext("guid") or link),
                    "source": "Slickdeals",
                    "title": title,
                    "price": price,
                    "was": None,
                    "url": link,
                }
            )
    return deals


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def notify(deal):
    was = f" (was ${deal['was']:,.0f})" if deal.get("was") else ""
    msg = f"[{deal['source']}] ${deal['price']:,.0f}{was}\n{deal['title']}\n{deal['url']}"
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
        # alert on first sight, or if the price has dropped since last alert
        if prev is None or d["price"] < prev["price"]:
            notify(d)
            state[d["id"]] = {"price": d["price"], "title": d["title"]}
            new += 1
    save_state(state)
    print(f"{time.strftime('%H:%M:%S')} checked {len(candidates)} listings, {new} new deal(s).")


def main():
    if "--test" in sys.argv:
        notify({"source": "Test", "title": "If you see this, alerts work!", "price": 0, "was": None, "url": "https://example.com"})
        return
    if "--once" in sys.argv:
        check_once()
        return
    print(f"Watching for RTX 5070+ gaming desktops ≤ ${MAX_PRICE:,.0f} every {CHECK_MINUTES:g} min. Ctrl+C to stop.")
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

