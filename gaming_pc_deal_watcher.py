#!/usr/bin/env python3
"""
Gaming PC Deal Watcher — verified listings only.

Important:
- Best Buy's API is the only alert source in strict active-deal mode.
- Slickdeals RSS is deliberately NOT used to send alerts: its title price and
  post can be stale, and there is no universal way to verify every retailer.
- Availability is checked against Best Buy's near-real-time product API.
  Store-specific/local pickup availability is not guaranteed by this version.

GPU matching: NVIDIA RTX 5070/5070 Ti/5080/5090 and AMD RX 9070/9070 XT.
Scores and fair-price estimates are transparent heuristics, not benchmark
guarantees or live price quotes.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

MAX_PRICE = float(os.getenv("MAX_PRICE", "1500"))
CHECK_MINUTES = float(os.getenv("CHECK_MINUTES", "15"))
BESTBUY_API_KEY = os.getenv("BESTBUY_API_KEY", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
STATE_FILE = Path(__file__).with_name("seen_deals.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; GamingPCDealWatcher/2.0)"}

EXCLUDE_RE = re.compile(
    r"laptop|notebook|\bmobile\b|monitor|motherboard|graphics card|video card|"
    r"\bgpu only\b|handheld|mini ?pc|barebone|graphics card|"
    r"\bcase\b only",
    re.I,
)
DESKTOP_RE = re.compile(
    r"desktop|gaming pc|\bpc\b|tower|prebuilt|pre-built|\bomen\b|alienware|aurora|"
    r"gaming desktop",
    re.I,
)

# Ordered from strongest to weakest; names must appear in the product title.
GPU_PROFILES = [
    (re.compile(r"\bRTX\s*5090\b", re.I), "RTX 5090", 9.8, 3000),
    (re.compile(r"\bRTX\s*5080\b", re.I), "RTX 5080", 8.8, 2000),
    (re.compile(r"\bRTX\s*5070\s*Ti\b", re.I), "RTX 5070 Ti", 8.1, 1650),
    (re.compile(r"\bRTX\s*5070\b", re.I), "RTX 5070", 7.3, 1350),
    (re.compile(r"\bRX\s*9070\s*XT\b", re.I), "Radeon RX 9070 XT", 8.3, 1550),
    (re.compile(r"\bRX\s*9070\b", re.I), "Radeon RX 9070", 7.6, 1400),
]


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def gpu_profile(title):
    for pattern, label, base_score, fair_price in GPU_PROFILES:
        if pattern.search(title):
            return {"gpu": label, "base_score": base_score, "fair_price": fair_price}
    return None


def looks_like_match(title):
    return (
        gpu_profile(title) is not None
        and not EXCLUDE_RE.search(title)
        and bool(DESKTOP_RE.search(title))
    )


def score_pc(title, price, profile):
    """Heuristic 1–10 score based on identifiable GPU and title specs."""
    score = profile["base_score"]
    t = title.lower()
    # Small bonuses only when the title explicitly states the spec.
    if re.search(r"\b(i[7-9]-\d{4,5}|core ultra 7|core ultra 9|ryzen 7|ryzen 9)\b", t):
        score += 0.35
    elif re.search(r"\b(i5-\d{4,5}|core ultra 5|ryzen 5)\b", t):
        score += 0.10
    if re.search(r"\b32\s*gb\b", t):
        score += 0.25
    elif re.search(r"\b16\s*gb\b", t):
        score -= 0.10
    if re.search(r"\b(2\s*tb|2000\s*gb)\b", t):
        score += 0.15
    # Price value contributes at most +0.5 or -0.5, relative to rough fair value.
    fair = profile["fair_price"]
    ratio = price / fair if fair else 1
    if ratio <= 0.75:
        score += 0.50
    elif ratio <= 0.90:
        score += 0.30
    elif ratio <= 1.00:
        score += 0.10
    elif ratio >= 1.25:
        score -= 0.50
    elif ratio >= 1.10:
        score -= 0.25
    return round(max(1.0, min(10.0, score)), 1)


def fetch_bestbuy():
    if not BESTBUY_API_KEY:
        print("[Best Buy] No API key configured; no alerts will be sent.")
        return []
    deals, seen_skus = [], set()
    # Include AMD alternatives as well as NVIDIA models.
    searches = ("5070", "5070 Ti", "5080", "5090", "RX 9070", "RX 9070 XT")
    for model in searches:
        # API search terms can match non-desktops; title filters below are strict.
        q = f"(search=desktop&search={urllib.parse.quote(model)}&salePrice<={MAX_PRICE:.0f}&onlineAvailability=true&orderable=true)"
        url = (
            "https://api.bestbuy.com/v1/products"
            + urllib.parse.quote(q, safe="=&()<>")
            + "?"
            + urllib.parse.urlencode({
                "apiKey": BESTBUY_API_KEY,
                "format": "json",
                "show": "sku,name,salePrice,regularPrice,url,onlineAvailability,orderable,inStorePickup",
                "pageSize": 100,
            })
        )
        try:
            data = json.loads(http_get(url))
        except Exception as e:
            print(f"[Best Buy] {model} lookup failed: {e}")
            continue
        for p in data.get("products", []):
            sku = p.get("sku")
            if not sku or sku in seen_skus:
                continue
            seen_skus.add(sku)
            title = p.get("name", "")
            price = p.get("salePrice")
            # Require explicit positive availability and a sane current price.
            if (
                not isinstance(price, (int, float))
                or price < 400
                or price > MAX_PRICE
                or p.get("onlineAvailability") is not True
                or p.get("orderable") is not True
                or not looks_like_match(title)
            ):
                continue
            deals.append({
                "id": f"bby-{sku}",
                "source": "Best Buy (live API)",
                "sku": sku,
                "title": title,
                "price": float(price),
                "was": p.get("regularPrice"),
                "url": p.get("url") or f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}",
                "online_available": True,
                "pickup_available": p.get("inStorePickup") is True,
            })
    return deals


def verify_bestbuy_listing(deal):
    """Re-fetch this SKU just before alerting to reduce stale-price alerts."""
    sku = deal.get("sku")
    if not sku or not BESTBUY_API_KEY:
        return None
    params = urllib.parse.urlencode({
        "apiKey": BESTBUY_API_KEY,
        "format": "json",
        "show": "sku,name,salePrice,regularPrice,url,onlineAvailability,orderable,inStorePickup",
    })
    url = f"https://api.bestbuy.com/v1/products/{urllib.parse.quote(str(sku))}.json?{params}"
    try:
        data = json.loads(http_get(url))
        if (
            data.get("onlineAvailability") is not True
            or data.get("orderable") is not True
            or not isinstance(data.get("salePrice"), (int, float))
            or data["salePrice"] < 400
            or data["salePrice"] > MAX_PRICE
            or not looks_like_match(data.get("name", ""))
        ):
            return None
        deal.update({
            "title": data.get("name", deal["title"]),
            "price": float(data["salePrice"]),
            "was": data.get("regularPrice"),
            "url": data.get("url") or deal["url"],
            "pickup_available": data.get("inStorePickup") is True,
        })
        return deal
    except Exception as e:
        print(f"[Best Buy] live verification failed for SKU {sku}: {e}")
        return None


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def notify(deal):
    profile = gpu_profile(deal["title"])
    if not profile:
        return
    score = score_pc(deal["title"], deal["price"], profile)
    fair = profile["fair_price"]
    delta = fair - deal["price"]
    if delta >= 0:
        value_line = f"Estimated value: ~${fair:,.0f} | ~${delta:,.0f} below estimate"
    else:
        value_line = f"Estimated value: ~${fair:,.0f} | ~${-delta:,.0f} above estimate"
    was = deal.get("was")
    sale_line = f" (regular ${was:,.0f})" if isinstance(was, (int, float)) and was > deal["price"] else ""
    pickup_line = "Best Buy pickup: listed as eligible (local stock not confirmed)" if deal.get("pickup_available") else "Best Buy pickup: not confirmed"
    msg = (
        f"🎮 **VERIFIED GAMING PC DEAL — {score:.1f}/10**\n"
        f"**Price:** ${deal['price']:,.2f}{sale_line}\n"
        f"**GPU:** {profile['gpu']}\n"
        f"**Gaming PC score:** {score:.1f}/10 *(heuristic; GPU + listed specs + value)*\n"
        f"**{value_line}** *(rough prebuilt estimate, not a live market quote)*\n"
        f"**Availability:** online orderable at last API check; {pickup_line}\n"
        f"**{deal['title']}**\n"
        f"{deal['url']}"
    )
    print("\n" + msg + "\n")
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=msg.encode("utf-8"),
                headers={"Title": f"Gaming PC deal {score:.1f}/10", "Click": deal["url"], **UA},
            )
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as e:
            print(f"[ntfy] failed: {e}")
    if DISCORD_WEBHOOK:
        try:
            req = urllib.request.Request(
                DISCORD_WEBHOOK,
                data=json.dumps({"content": msg, "allowed_mentions": {"parse": []}}).encode("utf-8"),
                headers={"Content-Type": "application/json", **UA},
            )
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as e:
            print(f"[Discord] failed: {e}")


def check_once():
    state = load_state()
    candidates = fetch_bestbuy()
    new = 0
    for candidate in candidates:
        d = verify_bestbuy_listing(candidate)
        if not d:
            print(f"[Skip] SKU {candidate.get('sku')} no longer passes live availability/price check.")
            continue
        prev = state.get(d["id"])
        # Alert on first sight or a genuine price decrease, but only after revalidation.
        if prev is None or d["price"] < prev.get("price", float("inf")):
            notify(d)
            state[d["id"]] = {
                "price": d["price"],
                "title": d["title"],
                "last_verified": int(time.time()),
            }
            new += 1
        else:
            # Refresh metadata even when not alerting.
            state[d["id"]]["last_verified"] = int(time.time())
    save_state(state)
    print(f"{time.strftime('%H:%M:%S')} checked {len(candidates)} Best Buy candidates, {new} new deal(s).")
    if not BESTBUY_API_KEY:
        print("Configure BESTBUY_API_KEY in GitHub Actions secrets to enable verified alerts.")


def main():
    if "--test" in sys.argv:
        notify({
            "source": "Test",
            "title": "Gaming desktop RTX 5070 Ryzen 7 32GB 2TB SSD",
            "price": 999,
            "was": 1499,
            "url": "https://example.com",
            "pickup_available": False,
        })
        return
    if "--once" in sys.argv:
        check_once()
        return
    print(f"Watching verified Best Buy gaming desktops ≤ ${MAX_PRICE:,.0f} every {CHECK_MINUTES:g} min.")
    print("Slickdeals RSS is intentionally excluded from alerts because its prices may be stale.")
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"Check failed: {e}")
        time.sleep(CHECK_MINUTES * 60)


if __name__ == "__main__":
    main()
