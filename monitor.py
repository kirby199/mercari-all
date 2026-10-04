#!/usr/bin/env python3
"""Mercari Japan new-listing monitor.

Reads keywords from searches.txt, loads each Mercari search (newest first) in a
headless browser, compares against seen.json, and emails any new listings
(name + price + link) in a single message.

Usage:
    python monitor.py              # normal run
    python monitor.py --dry-run    # print what would be emailed, change nothing
    python monitor.py --test-email # just send a test email (checks your secrets)

Environment variables:
    GMAIL_USER          Gmail address used to SEND the alert
    GMAIL_APP_PASSWORD  16-character Google "app password" for that account
    ALERT_TO            Recipient (default: roy.yn.zhang@gmail.com)
"""
import argparse
import json
import os
import smtplib
import sys
import time
import urllib.parse
from email.message import EmailMessage
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEARCHES_FILE = ROOT / "searches.txt"
STATE_FILE = ROOT / "seen.json"
DEBUG_DIR = ROOT / "debug"
MAX_SEEN_PER_SEARCH = 500   # remember the most recent N listing IDs per keyword
MAX_ITEMS_IN_EMAIL = 60
DEFAULT_RECIPIENT = "roy.yn.zhang@gmail.com"

ITEM_SELECTOR = 'a[href*="/item/m"], a[href*="/shops/product/"]'
EMPTY_MARKERS = (
    "出品された商品がありません",
    "見つかりませんでした",
    "No items found",
    "did not match any",
)

# Runs inside the page: pulls id / name / price out of each result card.
EXTRACT_JS = r"""
() => {
  const results = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/item/m"], a[href*="/shops/product/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/item\/(m\d+)/) || href.match(/\/shops\/product\/([A-Za-z0-9]+)/);
    if (!m || seen.has(m[1])) return;
    seen.add(m[1]);
    const cell = a.closest('[data-testid="item-cell"]') || a;
    const text = (cell.innerText || cell.textContent || '').trim();

    // --- name ---
    let name = '';
    const nameEl = cell.querySelector('[data-testid="thumbnail-item-name"]');
    if (nameEl) name = (nameEl.innerText || nameEl.textContent || '').trim();
    let label = '';
    const labelled = cell.querySelector('[role="img"][aria-label], [aria-label], img[alt]');
    if (labelled) label = (labelled.getAttribute('aria-label') || labelled.getAttribute('alt') || '').trim();
    if (!name && label) {
      name = label.replace(/の(画像|サムネイル).*$/, '').replace(/\s+(image|thumbnail)\b.*$/i, '').trim();
    }
    if (!name) {
      const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
      name = lines.find(l => !/^[¥￥]?\s*[\d,]+\s*(円)?$/.test(l)) || '';
    }

    // --- price ---
    let price = null;
    const priceEl = cell.querySelector('[class*="merPrice"], [data-testid="price"]');
    if (priceEl) {
      const digits = (priceEl.innerText || priceEl.textContent || '').replace(/[^\d]/g, '');
      if (digits) price = parseInt(digits, 10);
    }
    if (price === null) {
      const pm = (label + ' ' + text).match(/[¥￥]\s*([\d,]+)|([\d,]+)\s*(?:円|yen)/i);
      if (pm) price = parseInt((pm[1] || pm[2]).replace(/,/g, ''), 10);
    }
    results.push({ id: m[1], name: name, price: price });
  });
  return results;
}
"""


class ScrapeError(Exception):
    pass


# ----------------------------------------------------------------- helpers
def load_searches():
    if not SEARCHES_FILE.exists():
        sys.exit("searches.txt not found")
    out = []
    for line in SEARCHES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("WARNING: seen.json is corrupt; starting fresh", file=sys.stderr)
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def scrape_url(keyword):
    q = urllib.parse.quote(keyword)
    return f"https://jp.mercari.com/search?keyword={q}&sort=created_time&order=desc"


def view_url(keyword):
    """Search link placed in the email (English UI, same as the one you use)."""
    q = urllib.parse.quote(keyword)
    return f"https://jp.mercari.com/en/search?keyword={q}&sort=created_time&order=desc"


def item_url(item_id):
    if item_id.startswith("m") and item_id[1:].isdigit():
        return f"https://jp.mercari.com/en/item/{item_id}"
    return f"https://jp.mercari.com/en/shops/product/{item_id}"


def fmt_price(price):
    return f"¥{price:,}" if isinstance(price, int) else "price n/a"


# ---------------------------------------------------------------- scraping
def extract_items(page):
    return page.evaluate(EXTRACT_JS)


def scrape_keyword(context, keyword, attempts=3):
    last_err = None
    for attempt in range(1, attempts + 1):
        page = context.new_page()
        try:
            page.goto(scrape_url(keyword), wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector(ITEM_SELECTOR, timeout=30000)
            except Exception:
                body = page.inner_text("body") if page.query_selector("body") else ""
                if any(marker in body for marker in EMPTY_MARKERS):
                    return []  # genuinely zero results
                raise ScrapeError("no listings appeared (blocked, layout change, or slow load)")
            page.wait_for_timeout(1500)  # let the grid finish hydrating
            items = extract_items(page)
            if not items:
                raise ScrapeError("listing links present but nothing extracted")
            return items
        except Exception as e:  # noqa: BLE001
            last_err = e
            DEBUG_DIR.mkdir(exist_ok=True)
            slug = "".join(c if c.isalnum() else "_" for c in keyword)[:40] or "search"
            try:
                (DEBUG_DIR / f"{slug}.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=False)
            except Exception:  # noqa: BLE001
                pass
            print(f"[{keyword}] attempt {attempt}/{attempts} failed: {e}", file=sys.stderr)
            time.sleep(3 * attempt)
        finally:
            page.close()
    raise ScrapeError(f"{keyword}: {last_err}")


# ------------------------------------------------------------------- email
def build_email(new_by_keyword, recipient, sender):
    total = sum(len(v) for v in new_by_keyword.values())
    kws = ", ".join(new_by_keyword)
    msg = EmailMessage()
    msg["Subject"] = f"Mercari: {total} new listing{'s' if total != 1 else ''} ({kws})"
    msg["From"] = sender
    msg["To"] = recipient

    text_parts, html_parts = [], []
    shown = 0
    for kw, items in new_by_keyword.items():
        text_parts.append(f"== {kw} ==  {view_url(kw)}")
        html_parts.append(
            f'<h3 style="margin:16px 0 6px"><a href="{escape(view_url(kw))}">{escape(kw)}</a></h3><ul style="padding-left:18px">'
        )
        for it in items:
            if shown >= MAX_ITEMS_IN_EMAIL:
                break
            shown += 1
            name = it.get("name") or "(no title)"
            url = item_url(it["id"])
            price = fmt_price(it.get("price"))
            text_parts.append(f"- {name}\n  {price}\n  {url}")
            html_parts.append(
                f'<li style="margin-bottom:8px"><a href="{escape(url)}">{escape(name)}</a><br>'
                f'<b>{escape(price)}</b></li>'
            )
        html_parts.append("</ul>")
        text_parts.append("")
    if total > shown:
        extra = f"...and {total - shown} more — open the search link to see them all."
        text_parts.append(extra)
        html_parts.append(f"<p>{escape(extra)}</p>")

    msg.set_content("\n".join(text_parts))
    msg.add_alternative(
        '<div style="font-family:system-ui,sans-serif;font-size:15px">' + "".join(html_parts) + "</div>",
        subtype="html",
    )
    return msg


def send(msg):
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD are not set")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw.replace(" ", ""))
        s.send_message(msg)


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    args = ap.parse_args()

    recipient = os.environ.get("ALERT_TO") or DEFAULT_RECIPIENT
    sender = os.environ.get("GMAIL_USER", "")

    if args.test_email:
        msg = EmailMessage()
        msg["Subject"] = "Mercari monitor: test email"
        msg["From"] = sender
        msg["To"] = recipient
        msg.set_content("If you can read this, email alerts are working.")
        send(msg)
        print(f"Test email sent to {recipient}")
        return 0

    from playwright.sync_api import sync_playwright

    searches = load_searches()
    state = load_state()
    new_state = dict(state)
    new_by_keyword = {}
    failures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        for i, kw in enumerate(searches):
            if i:
                time.sleep(3)
            try:
                items = scrape_keyword(context, kw)
            except ScrapeError as e:
                failures.append(str(e))
                continue
            ids = [it["id"] for it in items]
            print(f"[{kw}] {len(items)} listings on page")
            if kw not in state:
                print(f"[{kw}] first run for this keyword: recording baseline, no email")
                new_state[kw] = ids[:MAX_SEEN_PER_SEARCH]
                continue
            seen = set(state[kw])
            fresh = [it for it in items if it["id"] not in seen]
            if fresh:
                new_by_keyword[kw] = fresh
                print(f"[{kw}] {len(fresh)} NEW")
            new_state[kw] = ([it["id"] for it in fresh] + state[kw])[:MAX_SEEN_PER_SEARCH]
        browser.close()

    if new_by_keyword:
        msg = build_email(new_by_keyword, recipient, sender)
        if args.dry_run:
            print("--- DRY RUN: would send ---")
            print(msg.get_body(("plain",)).get_content())
        else:
            send(msg)  # raises on failure -> state not saved -> retried next run
            print(f"Email sent to {recipient}")

    if not args.dry_run:
        save_state(new_state)

    if failures:
        print("FAILED searches:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
