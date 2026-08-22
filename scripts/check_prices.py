#!/usr/bin/env python3
"""Check BaNANA (bnn.in.th) iPhone/iPad prices and notify via LINE on change."""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "prices.json"
CONFIG_FILE = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "check_prices.log"
RUN_STATE_FILE = BASE_DIR / "last_run.json"

# The daily check is scheduled for this time; the catch-up job (--if-needed)
# only steps in after it, so a failed run is retried the moment net is back.
SCHEDULED_HOUR, SCHEDULED_MINUTE = 11, 30

CATEGORIES = {
    "iPhone": "https://www.bnn.in.th/th/p/apple/apple-iphone",
    "iPad": "https://www.bnn.in.th/th/p/apple/apple-ipad",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.bnn.in.th/th",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}

# Each product is one <a class="product-link ... product-item"> card. Name and
# price are pulled from inside a single card only: a regex that scanned across
# card boundaries used to pair a model with its neighbour's price, which showed
# up as false "price changed" alerts (e.g. iPhone 16e reported at iPhone 15's price).
CARD_START_RE = re.compile(
    r'<a href="/th/p/(?P<slug>[^"?]+)\?ref=category[^"]*"\s+class="product-link[^"]*product-item"'
)
NAME_RE = re.compile(r'class="product-name"[^>]*>\s*(?P<name>[^<]+?)\s*</div>', re.S)
PRICE_RE = re.compile(
    r'class="product-price"[^>]*>\s*(?:<span[^>]*>)?\s*(?P<price>[^<]+?)\s*'
    r'(?:</span>)?\s*(?:<!---->)?\s*</div>',
    re.S,
)
# "ประหยัดไป ฿3,200" — the campaign discount already reflected in product-price.
SAVE_RE = re.compile(r'ประหยัดไป\s*<strong[^>]*>฿(?P<save>[\d,]+)', re.S)
BAHT_RE = re.compile(r"฿\s*([\d,]+)")


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")
    print(msg)


def fetch(url):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_products(html, category):
    """Parse one product per card, never letting a card borrow its neighbour's price."""
    products = {}
    matches = list(CARD_START_RE.finditer(html))
    for m, nxt in zip(matches, matches[1:] + [None]):
        card = html[m.end():nxt.start() if nxt else len(html)]
        name = NAME_RE.search(card)
        price = PRICE_RE.search(card)
        if not name or not price:
            continue  # not a product card, or markup changed

        slug = m.group("slug")
        save = SAVE_RE.search(card)
        products[slug] = {
            "name": name.group("name").strip(),
            "price": " ".join(price.group("price").split()),
            "save": int(save.group("save").replace(",", "")) if save else 0,
            "out_of_stock": "สินค้าหมด" in card,
            "url": f"https://www.bnn.in.th/th/p/{slug}",
            "category": category,
        }
    return products


def baht_values(price_text):
    return [int(v.replace(",", "")) for v in BAHT_RE.findall(price_text or "")]


def is_discount_flip(old, new):
    """True when the two prices differ only by the campaign discount.

    bnn.in.th sometimes serves the listing with the "ประหยัดไป" discount already
    applied and sometimes without it. Every number in the range then shifts by
    exactly the discount, which is not a real price change and must not alert.
    """
    old_save, new_save = old.get("save", 0), new.get("save", 0)
    # Exactly one side must be the undiscounted render, and it must be the dearer one.
    if (old_save == 0) == (new_save == 0):
        return False

    old_values, new_values = baht_values(old.get("price")), baht_values(new.get("price"))
    if not old_values or len(old_values) != len(new_values):
        return False

    shifts = {o - n for o, n in zip(old_values, new_values)}
    if len(shifts) != 1:
        return False  # the numbers moved independently, so it is a real change
    shift = shifts.pop()

    discount = old_save or new_save
    # shift is old - new, so the undiscounted (dearer) side decides the sign.
    dearer_is_undiscounted = shift < 0 if new_save == 0 else shift > 0
    return abs(shift) == discount and dearer_is_undiscounted


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def last_success_date():
    if RUN_STATE_FILE.exists():
        return json.loads(RUN_STATE_FILE.read_text(encoding="utf-8")).get("last_success", "")
    return ""


def record_success():
    RUN_STATE_FILE.write_text(
        json.dumps({"last_success": datetime.now().strftime("%Y-%m-%d")}), encoding="utf-8"
    )


def should_run_catchup():
    """True only when today's scheduled check hasn't succeeded yet."""
    now = datetime.now()
    scheduled = now.replace(
        hour=SCHEDULED_HOUR, minute=SCHEDULED_MINUTE, second=0, microsecond=0
    )
    if now < scheduled:
        return False
    return last_success_date() != now.strftime("%Y-%m-%d")


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_line_message(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token and CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        token = config.get("line_channel_access_token")
    if not token:
        log("No LINE token (config.json or LINE_CHANNEL_ACCESS_TOKEN env var), "
            "skipping notification. Message was:\n" + text)
        return

    # Broadcast: sends to every friend of the LINE Official Account, not just one user.
    body = json.dumps({
        "messages": [{"type": "text", "text": text[:5000]}],
    }).encode("utf-8")
    req = Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            resp.read()
    except HTTPError as e:
        log(f"LINE broadcast failed: {e.code} {e.read().decode('utf-8', errors='replace')}")


def main():
    # Catch-up mode: stay silent unless today's check still needs to happen.
    if "--if-needed" in sys.argv and not should_run_catchup():
        return

    old_state = load_state()
    new_state = dict(old_state)
    changes = []
    flips = []
    errors = []

    for category, url in CATEGORIES.items():
        try:
            html = fetch(url)
        except (URLError, HTTPError) as e:
            errors.append(f"{category}: fetch failed ({e})")
            continue

        products = parse_products(html, category)
        if not products:
            errors.append(f"{category}: 0 products parsed, page structure may have changed")
            continue

        for slug, product in products.items():
            old = old_state.get(slug)
            new_state[slug] = product
            if old is None:
                continue

            price_moved = old["price"] != product["price"]
            stock_moved = old["out_of_stock"] != product["out_of_stock"]
            if price_moved and not stock_moved and is_discount_flip(old, product):
                # Keep the discounted figure on file so the saved price matches
                # what a shopper actually sees on the site.
                if not product["save"]:
                    new_state[slug] = dict(product, price=old["price"], save=old.get("save", 0))
                flips.append(f"{product['name']}: {old['price']} / {product['price']}")
                continue
            if price_moved or stock_moved:
                changes.append((old, product))

    if flips:
        log("ข้ามส่วนลดที่เว็บสลับไปมา (ไม่ใช่ราคาเปลี่ยนจริง): " + "; ".join(flips))

    price_changes = [(o, n) for o, n in changes if o["price"] != n["price"]]
    stock_changes = [(o, n) for o, n in changes if o["price"] == n["price"]]

    if changes:
        lines = ["ราคา BaNANA เปลี่ยนแปลง:"]
        for old, new in price_changes:
            stock_note = ""
            if old["out_of_stock"] != new["out_of_stock"]:
                stock_note = " (มีสินค้าแล้ว)" if not new["out_of_stock"] else " (สินค้าหมด)"
            lines.append(
                f"\n{new['name']}\n{old['price']} -> {new['price']}{stock_note}\n{new['url']}"
            )
        for old, new in stock_changes:
            status = "มีสินค้าแล้ว" if not new["out_of_stock"] else "สินค้าหมด"
            lines.append(
                f"\n{new['name']} — {status} (ราคา {new['price']})\n{new['url']}"
            )
        message = "\n".join(lines)
        log(message)
        send_line_message(message)
    elif errors:
        log("No price changes detected, but fetch had errors: " + "; ".join(errors))
    else:
        log(f"No price changes ({len(new_state)} products tracked).")

    if not errors:
        record_success()

    parser_errors = [e for e in errors if "page structure may have changed" in e]
    if parser_errors:
        alert = (
            "⚠️ ตรวจราคา BaNANA ไม่ได้ เว็บอาจเปลี่ยนโครงสร้างหน้า ต้องแก้สคริปต์:\n"
            + "\n".join(parser_errors)
        )
        log(alert)
        send_line_message(alert)

    save_state(new_state)
    rebuild_dashboard()
    publish_site()


def rebuild_dashboard():
    """Refresh dashboard.html so the summary page always reflects the last run."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import build_dashboard

        build_dashboard.main()
    except Exception as e:  # never let the dashboard break the price check
        log(f"Dashboard build failed: {e}")


def publish_site():
    """Commit this run and push, so the GitHub Pages copy stays current.

    Skipped silently when there is no 'origin' remote configured, so the
    script keeps working on a machine that is not set up for publishing.
    """
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(BASE_DIR), *args],
            capture_output=True, text=True, timeout=120,
        )

    try:
        if git("remote", "get-url", "origin").returncode != 0:
            return
        if not git("status", "--porcelain").stdout.strip():
            return  # nothing changed this run

        git("add", "-A", ".")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        commit = git("commit", "-m", f"Price check {stamp}")
        if commit.returncode != 0:
            log(f"Publish skipped, commit failed: {commit.stderr.strip()[:200]}")
            return
        push = git("push")
        if push.returncode != 0:
            log(f"Publish skipped, push failed: {push.stderr.strip()[:200]}")
    except Exception as e:  # publishing must never break the price check
        log(f"Publish failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
