"""
Rental listing monitor — works for Kereby, CEJ, and any JS-rendered site.

Loads the page with a real browser, optionally scopes to a CSS selector,
strips common volatile noise, hashes the result, and notifies via ntfy.sh
if anything changed.

Runs as a long-lived loop inside a GitHub Actions job (see
.github/workflows/check.yml and check-cej.yml). Each workflow triggers
itself at the end so checks happen reliably every ~30 seconds.
"""

import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta

import requests
from playwright.sync_api import sync_playwright

# All settings configurable via environment variables (set in each workflow)
URL            = os.environ.get("MONITOR_URL",            "https://kerebyudlejning.dk/")
SNAPSHOT_FILE  = os.environ.get("SNAPSHOT_FILE",          "last_snapshot.txt")
HASH_FILE      = os.environ.get("HASH_FILE",              "last_hash.txt")
NTFY_TOPIC     = os.environ.get("NTFY_TOPIC",             "")
NOTIFY_TITLE   = os.environ.get("NOTIFY_TITLE",           "Listing change!")
NOTIFY_BODY    = os.environ.get("NOTIFY_BODY",            f"The rental page changed — check it now: {URL}")
SELECTOR       = os.environ.get("SELECTOR",               "")   # optional CSS selector
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "30"))
LOOP_MINUTES   = int(os.environ.get("LOOP_DURATION_MINUTES",  "58"))

# Lines matching these patterns change every page load and carry no listing
# information — strip them before hashing to avoid false positives.
_NOISE = re.compile(
    r"^\s*("
    r"\d{10,}"                                                              # unix timestamps / long IDs
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"    # UUIDs
    r"|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"                                     # ISO datetimes
    r"|[A-Za-z0-9+/]{40,}={0,2}"                                           # base64 tokens
    r")\s*$"
)


def fetch_listing_text() -> str:
    """Render the page with a real browser and return cleaned visible text."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        if SELECTOR:
            try:
                page.wait_for_selector(SELECTOR, timeout=5000)
                text = page.inner_text(SELECTOR)
                print(f"  → Extracted text from selector: {SELECTOR!r}")
            except Exception:
                print(f"  → Selector {SELECTOR!r} not found, falling back to <body>")
                text = page.inner_text("body")
        else:
            text = page.inner_text("body")

        browser.close()

    # Drop noisy lines that change every load
    clean = "\n".join(l for l in text.splitlines() if not _NOISE.match(l))
    return clean


def send_notification(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        print("  → No NTFY_TOPIC set, skipping push notification.")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "house"},
        timeout=15,
    )


def load_old_hash() -> str:
    if os.path.exists(HASH_FILE):
        return open(HASH_FILE, encoding="utf-8").read().strip()
    return ""


def save_snapshot(text: str, new_hash: str) -> None:
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        f.write(new_hash)
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def check_once(old_hash: str) -> str:
    try:
        text = fetch_listing_text()
    except Exception as exc:
        print(f"  → Error loading page: {exc}", file=sys.stderr)
        return old_hash

    new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    if not old_hash:
        print("  → First ever check — saving baseline. No notification sent.")
        save_snapshot(text, new_hash)
    elif new_hash != old_hash:
        print("  → *** CHANGE DETECTED ***")
        send_notification(NOTIFY_TITLE, NOTIFY_BODY)
        save_snapshot(text, new_hash)
    else:
        print("  → No change.")

    return new_hash


def main() -> int:
    deadline = datetime.utcnow() + timedelta(minutes=LOOP_MINUTES)
    print(
        f"Starting monitor loop: checking every {CHECK_INTERVAL}s "
        f"for {LOOP_MINUTES} minutes (until ~{deadline.strftime('%H:%M')} UTC).\n"
        f"  URL:      {URL}\n"
        f"  Selector: {SELECTOR or '(full page)'}\n"
        f"  Snapshot: {HASH_FILE}"
    )

    current_hash = load_old_hash()
    run = 0

    while datetime.utcnow() < deadline:
        run += 1
        print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Check #{run}", flush=True)
        current_hash = check_once(current_hash)

        next_check = datetime.utcnow() + timedelta(seconds=CHECK_INTERVAL)
        if next_check < deadline:
            time.sleep(CHECK_INTERVAL)
        else:
            break

    print(f"Loop complete after {run} checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
