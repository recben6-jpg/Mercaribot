import requests
import time
import json
import os
import sys
import uuid
import logging
from datetime import datetime

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
REQUEST_DELAY   = float(os.environ.get("REQUEST_DELAY",  "12"))

SEARCH_TERMS = [
    "フルアート まとめ",
    "SR まとめ",
    "ポケモン カード まとめ",
    "ポケモン レア まとめ",
]

SEEN_FILE   = "seen_ids.json"
MAX_SEEN    = 5000
MAX_RETRIES = 3
RETRY_DELAY = 15

MERCARI_URL = "https://api.mercari.jp/v2/entities:search"

CONDITION_MAP = {
    "ITEM_CONDITION_ID_1": "New",
    "ITEM_CONDITION_ID_2": "Like new",
    "ITEM_CONDITION_ID_3": "Good",
    "ITEM_CONDITION_ID_4": "Fair",
    "ITEM_CONDITION_ID_5": "Poor",
}

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

# ─── MERCARI ──────────────────────────────────────────────────────────────────

def make_headers() -> dict:
    from mercari_dpop import generate_dpop_jwt
    dpop_token = generate_dpop_jwt(str(uuid.uuid4()), "POST", MERCARI_URL)
    return {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "application/json",
        "Accept-Language": "ja,en;q=0.9",
        "Content-Type":    "application/json",
        "X-Platform":      "web",
        "DPoP":            dpop_token,
        "Referer":         "https://jp.mercari.com/",
    }


def build_payload(keyword: str) -> dict:
    return {
        "pageSize": 30,
        "pageToken": "",
        "searchCondition": {
            "keyword":        keyword,
            "excludeKeyword": "",
            "sort":           "SORT_CREATED_TIME",
            "order":          "ORDER_DESC",
            "status":         ["STATUS_ON_SALE"],
            "categoryId":     [],
            "brandId":        [],
            "itemTypes":      [],
            "skuTypes":       [],
            "colorIds":       [],
            "priceMin":       0,
        },
        "defaultDatasets":    ["DATASET_TYPE_MERCARI"],
        "serviceFrom":        "suruga",
        "withItemBrand":      True,
        "withItemSize":       False,
        "withItemPromotions": False,
        "withItemSizes":      False,
        "withShopname":       False,
    }


def search_mercari(keyword: str) -> list:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(
                MERCARI_URL,
                json=build_payload(keyword),
                headers=make_headers(),
                timeout=20,
            )
            if r.status_code == 200:
                return r.json().get("items", [])
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
    condition = CONDITION_MAP.get(item.get("itemConditionId", ""), "Unknown")

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
        cycle_found  = 0
        cheapest_item: dict | None = None

        log.info(f"── Cycle {cycle} ──────────────────────")

        for keyword in SEARCH_TERMS:
            log.info(f"Searching: {keyword}")
            items = search_mercari(keyword)
            log.info(f"  {len(items)} results returned")

            for item in items:
                item_id = item.get("id")
                price   = item.get("price", 99_999)

                if not item_id:
                    continue

                # Track cheapest across all results
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
            log.info(f"Cycle {cycle} — nothing found. Cheapest: ¥{cheapest_item.get('price') if cheapest_item else 'N/A'}")
            notify_nothing_found(cycle, cheapest_item)
        else:
            log.info(f"Cycle {cycle} done — {cycle_found} hits (total: {total_found}).")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception as e:
        log.critical(f"Fatal error: {e}", exc_info=True)
        notify_error(f"Bot crashed: `{e}`\n\nCheck Railway logs.")
        sys.exit(1)
