import requests
import time
import json
import os
import sys
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

# Mercari category "ポケモンカードゲーム" — keeps One Piece, Duel Masters etc. out
POKEMON_CATEGORY_ID = os.environ.get("CATEGORY_ID", "1289")

# Anti-spam settings
MAX_ALERTS_PER_CYCLE = int(os.environ.get("MAX_ALERTS_PER_CYCLE", "10"))  # extra hits are summarised
NOTIFY_EMPTY   = os.environ.get("NOTIFY_EMPTY", "false").lower() == "true"  # "nothing found" message
NOTIFY_STARTUP = os.environ.get("NOTIFY_STARTUP", "true").lower() == "true"
DATA_DIR       = os.environ.get("DATA_DIR", ".")  # point at a Railway volume to keep seen IDs across deploys

SEEN_FILE    = os.path.join(DATA_DIR, "seen_ids.json")
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
    return f"https://jp.mercari.com/search?keyword={quote(keyword)}&category_id={POKEMON_CATEGORY_ID}&status=on_sale&sort=created_time&order=desc"


_PW = None
_BROWSER = None
_CONTEXT = None


def _get_context():
    """Lazily start one headless Chromium and reuse it across searches."""
    global _PW, _BROWSER, _CONTEXT
    if _CONTEXT is None:
        from playwright.sync_api import sync_playwright
        _PW = sync_playwright().start()
        _BROWSER = _PW.chromium.launch(args=["--no-sandbox"])
        _CONTEXT = _BROWSER.new_context(
            locale="ja-JP",
            user_agent=SESSION.headers["User-Agent"],
        )
    return _CONTEXT


def _reset_browser() -> None:
    global _PW, _BROWSER, _CONTEXT
    for obj, fn in ((_BROWSER, "close"), (_PW, "stop")):
        try:
            if obj:
                getattr(obj, fn)()
        except Exception:
            pass
    _PW = _BROWSER = _CONTEXT = None


def normalise_items(raw: list) -> list:
    items = []
    for item in raw:
        try:
            item_id = item.get("id", "")
            price = int(str(item.get("price", 0)).replace(",", "") or 0)
            if item_id and price:
                items.append({
                    "id":              item_id,
                    "name":            item.get("name", "Unknown"),
                    "price":           price,
                    "itemConditionId": str(item.get("itemConditionId", "")),
                    "thumbnails":      (item.get("thumbnails") or [])[:1],
                })
        except (ValueError, AttributeError):
            continue
    return items


def search_mercari(keyword: str) -> list:
    """Load the search page in a real browser and capture the search API response
    (the page is client-rendered, so there is no data in the raw HTML)."""
    url = build_url(keyword)

    for attempt in range(1, MAX_RETRIES + 1):
        page = None
        try:
            page = _get_context().new_page()
            captured = []

            def on_response(resp):
                if "entities:search" in resp.url and resp.status == 200:
                    try:
                        captured.append(resp.json())
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):  # up to ~30s for the search API call
                if captured:
                    break
                page.wait_for_timeout(1000)
            if captured:
                items = normalise_items(captured[-1].get("items", []))
                if items:
                    return items
                log.warning(f"Search API returned 0 items for '{keyword}' (attempt {attempt})")
            else:
                log.warning(f"No search API response seen for '{keyword}' (attempt {attempt}/{MAX_RETRIES})")
        except Exception as e:
            log.warning(f"Browser error for '{keyword}' (attempt {attempt}/{MAX_RETRIES}): {e}")
            _reset_browser()
        finally:
            try:
                if page:
                    page.close()
            except Exception:
                pass

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


def hit_embed(item: dict, keyword: str) -> dict:
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
    return embed


def notify_hits(hits: list) -> None:
    """Send all new hits of a cycle in as few Discord messages as possible
    (max 10 embeds per message), cheapest first, capped at MAX_ALERTS_PER_CYCLE."""
    hits = sorted(hits, key=lambda h: h[0].get("price", 0))
    shown, rest = hits[:MAX_ALERTS_PER_CYCLE], hits[MAX_ALERTS_PER_CYCLE:]
    embeds = [hit_embed(item, kw) for item, kw in shown]
    for i in range(0, len(embeds), 10):
        discord_post({"content": f"🎴 {len(hits)} new listing(s) under ¥{PRICE_THRESHOLD}" if i == 0 else "",
                      "embeds": embeds[i:i + 10]})
        time.sleep(1)
    if rest:
        discord_post({"content": f"…and {len(rest)} more under ¥{PRICE_THRESHOLD} (cheapest shown above). See the Railway logs."})


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
    if NOTIFY_STARTUP:
        notify_startup()

    seen        = load_seen()
    cycle       = 0
    total_found = 0

    while True:
        cycle += 1
        cycle_found   = 0
        cheapest_item = None
        new_hits      = []

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
                new_hits.append((item, keyword))
                seen.add(item_id)
                cycle_found += 1
                total_found += 1

            save_seen(seen)
            time.sleep(REQUEST_DELAY)

        if new_hits:
            notify_hits(new_hits)

        if cycle_found == 0:
            cheapest_price = cheapest_item.get("price") if cheapest_item else "N/A"
            log.info(f"Cycle {cycle} — nothing found. Cheapest: ¥{cheapest_price}")
            if NOTIFY_EMPTY:
                notify_nothing_found(cycle, cheapest_item)
        else:
            log.info(f"Cycle {cycle} done — {cycle_found} hits (total: {total_found}).")

        log.info(f"Sleeping {CHECK_INTERVAL}s — run 'fsearch' in Railway console to force now")
        interruptible_sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception as e:
        log.critical(f"Fatal error: {e}", exc_info=True)
        notify_error(f"Bot crashed: `{e}`\n\nCheck Railway logs.")
        sys.exit(1)
