"""
Kereby apartment listing monitor.

Loads the (JavaScript-rendered) Kereby rental page with Playwright,
extracts the visible listing text, and compares it to the last saved
snapshot. If it changed, sends a push notification via ntfy.sh.

Designed to run as a long-lived loop inside a single GitHub Actions job
(see .github/workflows/check.yml). The job is triggered hourly; this
script then loops every CHECK_INTERVAL_SECONDS for LOOP_DURATION_MINUTES,
giving near-real-time (30-second) coverage on GitHub's free tier.
"""

import hashlib
import os
import sys
import time
from datetime import datetime, timedelta

import requests
from playwright.sync_api import sync_playwright

URL = "https://kerebyudlejning.dk/"
SNAPSHOT_FILE = "last_snapshot.txt"
HASH_FILE = "last_hash.txt"

# Configurable via environment variables (set in the workflow)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "30"))
LOOP_MINUTES = int(os.environ.get("LOOP_DURATION_MINUTES", "58"))


def fetch_listing_text() -> str:
    """Render the page with a real browser and return the visible text."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=30000)
        # Give any client-side rendering a little extra time to settle
        page.wait_for_timeout(3000)
        text = page.inner_text("body")
        browser.close()
        return text


def send_notification(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        print("  → No NTFY_TOPIC set, skipping push notification.")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "house",
        },
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
    """Run a single check. Returns the new hash (may equal old_hash)."""
    try:
        text = fetch_listing_text()
    except Exception as exc:  # noqa: BLE001
        print(f"  → Error loading page: {exc}", file=sys.stderr)
        return old_hash  # treat as no change on transient errors

    new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    if not old_hash:
        print("  → First ever check — saving baseline. No notification sent.")
        save_snapshot(text, new_hash)
    elif new_hash != old_hash:
        print("  → *** CHANGE DETECTED ***")
        send_notification(
            "Kereby listing change!",
            "The Kereby rental page changed — check it now: " + URL,
        )
        save_snapshot(text, new_hash)
    else:
        print("  → No change.")

    return new_hash


def main() -> int:
    deadline = datetime.utcnow() + timedelta(minutes=LOOP_MINUTES)
    print(
        f"Starting monitor loop: checking every {CHECK_INTERVAL}s "
        f"for {LOOP_MINUTES} minutes (until ~{deadline.strftime('%H:%M')} UTC)."
    )

    current_hash = load_old_hash()
    run = 0

    while datetime.utcnow() < deadline:
        run += 1
        print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Check #{run}", flush=True)
        current_hash = check_once(current_hash)

        # Don't sleep past the deadline
        next_check = datetime.utcnow() + timedelta(seconds=CHECK_INTERVAL)
        if next_check < deadline:
            time.sleep(CHECK_INTERVAL)
        else:
            break

    print(f"Loop complete after {run} checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
