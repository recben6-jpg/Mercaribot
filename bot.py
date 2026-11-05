import requests
import time
import json
import os
import sys
import re
import logging
from datetime import datetime
from urllib.parse import quote

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mercari-bot")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
PRICE_THRESHOLD = int(os.environ.get("PRICE_THRESHOLD", "500"))
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL",  "300"))
REQUEST_DELAY   = float(os.environ.get("REQUEST_DELAY",  "15"))

SEARCH_TERMS = [
    "フルアート まとめ",
    "SR まとめ",
    "ポケモン カード まとめ",
    "ポケモン レア まとめ",
]

SEEN_FILE    = "seen_ids.json"
FORCE_FILE   = "force_search.flag"   # touch this file to trigger immediate search
MAX_SEEN     = 5000
MAX_RETRIES  = 3
RETRY_DELAY  = 15
SLEEP_TICK   = 5    # seconds between flag checks during sleep

CONDITION_MAP = {
    "1": "New",
    "2": "Like new",
    "3": "Good",
    "4": "Fair",
    "5": "Poor",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":  "ja,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
})

# ─── SEEN IDs ─────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Could not read seen file, starting fresh: {e}")
    return set()


def save_seen(seen: set) -> None:
    trimmed = list(seen)[-MAX_SEEN:]
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(trimmed, f)
    except OSError as e:
        log.warning(f"Could not save seen file: {e}")

# ─── FORCE SEARCH ─────────────────────────────────────────────────────────────

def check_force() -> bool:
    """Return True and remove the flag file if a force search was requested."""
    if os.path.exists(FORCE_FILE):
        try:
            os.remove(FORCE_FILE)
        except OSError:
            pass
        return True
    return False


def interruptible_sleep(seconds: int) -> bool:
    """Sleep in SLEEP_TICK chunks, return True immediately if force flag appears."""
    elapsed = 0
    while elapsed < seconds:
        if check_force():
            log.info("Force search flag detected — skipping remaining sleep")
            return True
        time.sleep(SLEEP_TICK)
        elapsed += SLEEP_TICK
    return False

# ─── MERCARI PAGE SCRAPE ──────────────────────────────────────────────────────

def build_url(keyword: str) -> str:
    return f"https://jp.mercari.com/search?keyword={quote(keyword)}&status=on_sale&sort=created_time&order=desc"


def parse_items_from_html(html: str) -> list:
    """Extract items from __NEXT_DATA__ JSON embedded in the page."""
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not match:
        log.warning("__NEXT_DATA__ not found in page")
        return []

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        log.warning(f"Failed to parse __NEXT_DATA__: {e}")
        return []

    # Walk the Next.js data tree to find items
    # Path can vary — try common locations
    items = []
    try:
        props = data.get("props", {}).get("pageProps", {})

        # Try direct items key
        raw = (
            props.get("items")
            or props.get("searchResult", {}).get("items")
            or props.get("initialData", {}).get("result", {}).get("items")
            or []
        )

        for item in raw:
            # Normalise fields across different response shapes
            item_id   = item.get("id") or item.get("itemId", "")
            name      = item.get("name") or item.get("itemName", "Unknown")
            price_raw = item.get("price") or item.get("itemPrice", 0)
            price     = int(str(price_raw).replace(",", "").replace("¥", "").strip()) if price_raw else 0
            condition = str(item.get("itemConditionId") or item.get("condition", {}).get("id", ""))
            thumb     = ""
            thumbs    = item.get("thumbnails") or item.get("itemImages") or []
            if thumbs:
                thumb = thumbs[0] if isinstance(thumbs[0], str) else thumbs[0].get("url", "")

            if item_id and price:
                items.append({
                    "id":              item_id,
                    "name":            name,
                    "price":           price,
                    "itemConditionId": condition,
                    "thumbnails":      [thumb] if thumb else [],
                })
    except Exception as e:
        log.warning(f"Error walking __NEXT_DATA__: {e}")

    return items


def search_mercari(keyword: str) -> list:
    url = build_url(keyword)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, timeout=25)
            if r.status_code == 200:
                items = parse_items_from_html(r.text)
                if items:
                    return items
                # Page returned OK but no items found in HTML structure
                log.warning(f"Page OK but 0 items parsed for '{keyword}' (attempt {attempt})")
            else:
                log.warning(f"HTTP {r.status_code} for '{keyword}' (attempt {attempt}/{MAX_RETRIES})")

        except requests.exceptions.Timeout:
            log.warning(f"Timeout for '{keyword}' (attempt {attempt}/{MAX_RETRIES})")
        except requests.exceptions.ConnectionError:
            log.warning(f"Connection error for '{keyword}' (attempt {attempt}/{MAX_RETRIES})")
        except Exception as e:
            log.error(f"Unexpected error for '{keyword}': {e}")
            break

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    return []

# ─── DISCORD ──────────────────────────────────────────────────────────────────

def discord_post(payload: dict) -> bool:
    if not DISCORD_WEBHOOK:
        log.warning("DISCORD_WEBHOOK not set — skipping notification")
        return False
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"Discord post failed: {e}")
        return False


def item_url(item: dict) -> str:
    return f"https://jp.mercari.com/item/{item.get('id', '')}"


def notify_hit(item: dict, keyword: str) -> None:
    price     = item.get("price", "?")
    name      = item.get("name", "Unknown item")
    url       = item_url(item)
    thumb     = (item.get("thumbnails") or [""])[0]
    condition = CONDITION_MAP.get(str(item.get("itemConditionId", "")), "Unknown")

    embed = {
        "title":       f"🎴  ¥{price}  —  under threshold",
        "description": f"**[{name}]({url})**",
        "url":         url,
        "color":       0x5865F2,
        "fields": [
            {"name": "Price",     "value": f"¥{price}",          "inline": True},
            {"name": "Threshold", "value": f"¥{PRICE_THRESHOLD}", "inline": True},
            {"name": "Condition", "value": condition,             "inline": True},
            {"name": "Query",     "value": f"`{keyword}`",        "inline": False},
        ],
        "footer": {"text": f"Mercari Japan Bot  •  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
    }
    if thumb:
        embed["thumbnail"] = {"url": thumb}

    discord_post({"embeds": [embed]})


def notify_nothing_found(cycle: int, cheapest: dict | None) -> None:
    if cheapest:
        price = cheapest.get("price", "?")
        name  = cheapest.get("name", "Unknown")[:60]
        url   = item_url(cheapest)
        note  = f"Closest listing: **¥{price}** — [{name}]({url})"
    else:
        note = "No listings returned — Mercari may be temporarily unavailable."

    discord_post({
        "embeds": [{
            "title":       f"🔍  Cycle {cycle} — nothing under ¥{PRICE_THRESHOLD}",
            "description": note,
            "color":       0x95a5a6,
            "footer":      {"text": f"Mercari Japan Bot  •  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
        }]
    })


def notify_startup() -> None:
    discord_post({
        "embeds": [{
            "title":       "✅  Mercari bot is running",
            "description": (
                f"Watching **{len(SEARCH_TERMS)} search terms** "
                f"for listings under **¥{PRICE_THRESHOLD}**.\n"
                f"Checks every **{CHECK_INTERVAL // 60} min**."
            ),
            "color": 0x57F287,
            "fields": [
                {"name": "Terms", "value": "\n".join(f"• `{t}`" for t in SEARCH_TERMS), "inline": False}
            ],
            "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        }]
    })


def notify_error(message: str) -> None:
    discord_post({
        "embeds": [{
            "title":       "⚠️  Bot error",
            "description": message,
            "color":       0xED4245,
            "footer":      {"text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        }]
    })

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run() -> None:
    if not DISCORD_WEBHOOK:
        log.error("DISCORD_WEBHOOK not set. Exiting.")
        sys.exit(1)

    log.info(f"Starting — threshold ¥{PRICE_THRESHOLD}, interval {CHECK_INTERVAL}s")
    notify_startup()

    seen        = load_seen()
    cycle       = 0
    total_found = 0

    while True:
        cycle += 1
        cycle_found   = 0
        cheapest_item = None

        log.info(f"── Cycle {cycle} ──────────────────────")

        for keyword in SEARCH_TERMS:
            log.info(f"Searching: {keyword}")
            items = search_mercari(keyword)
            log.info(f"  {len(items)} results parsed")

            for item in items:
                item_id = item.get("id")
                price   = item.get("price", 99_999)

                if not item_id:
                    continue

                if cheapest_item is None or price < cheapest_item.get("price", 99_999):
                    cheapest_item = item

                if item_id in seen:
                    continue
                if price > PRICE_THRESHOLD:
                    continue

                log.info(f"  → HIT  ¥{price}  {item.get('name', '')[:60]}")
                notify_hit(item, keyword)
                seen.add(item_id)
                cycle_found += 1
                total_found += 1

            save_seen(seen)
            time.sleep(REQUEST_DELAY)

        if cycle_found == 0:
            cheapest_price = cheapest_item.get("price") if cheapest_item else "N/A"
            log.info(f"Cycle {cycle} — nothing found. Cheapest: ¥{cheapest_price}")
            notify_nothing_found(cycle, cheapest_item)
        else:
            log.info(f"Cycle {cycle} done — {cycle_found} hits (total: {total_found}).")

        log.info(f"Sleeping {CHECK_INTERVAL}s — run 'fsearch' in Railway console to force now")
        forced = interruptible_sleep(CHECK_INTERVAL)
        if forced:
            discord_post({"embeds": [{"title": "⚡  Force search triggered", "color": 0xFEE75C,
                "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}]})


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception as e:
        log.critical(f"Fatal error: {e}", exc_info=True)
        notify_error(f"Bot crashed: `{e}`\n\nCheck Railway logs.")
        sys.exit(1)
