#!/usr/bin/env python
"""Load a URL in a browser pre-seeded with the cached session and log its traffic.

    python scripts/capture_web.py --url https://degreeaudit.oci.unw.edu/worksheets/WEB31 \
        --out captures/audit.jsonl --html captures/audit.html --wait 15000

Useful for reverse-engineering SPA endpoints without replaying the login flow.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console

from unw_planner.auth import BROWSER_UA, load_state
from unw_planner.config import CAPTURES_DIR, STATE_PATH

HOST_HINTS = ("unw.edu", "unwsp.edu", "microsoftonline.com")


def _seed_path() -> str:
    """Seed the browser with the cached jar so the page is already signed in."""
    return str(STATE_PATH)


def interesting(url: str) -> bool:
    return any(hint in urlparse(url).netloc for hint in HOST_HINTS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", default=str(CAPTURES_DIR / "web.jsonl"))
    parser.add_argument("--html", default=str(CAPTURES_DIR / "page.html"))
    parser.add_argument("--text", default="")
    parser.add_argument("--wait", type=int, default=12000)
    parser.add_argument("--click", action="append", default=[])
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    state = load_state()
    console = Console()
    out = Path(args.out)
    out.write_text("", encoding="utf-8")
    fh = out.open("a", encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=None if not state["cookies"] else _seed_path(),
            user_agent=BROWSER_UA,
            viewport={"width": 1600, "height": 1000},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        page = context.new_page()

        def on_request(request):
            if not interesting(request.url):
                return
            fh.write(json.dumps({
                "type": "request",
                "method": request.method,
                "url": request.url,
                "headers": {k: v for k, v in request.headers.items()
                            if k.lower() in ("accept", "content-type", "x-requested-with",
                                             "referer", "origin")},
                "body": request.post_data,
            }) + "\n")
            fh.flush()

        def on_response(response):
            if not interesting(response.url):
                return
            body = None
            ctype = response.headers.get("content-type", "")
            if any(k in ctype for k in ("json", "text", "html", "xml")):
                try:
                    body = response.text()[:8000]
                except Exception:
                    body = None
            fh.write(json.dumps({
                "type": "response",
                "status": response.status,
                "method": response.request.method,
                "url": response.url,
                "ctype": ctype,
                "body": body,
            }) + "\n")
            fh.flush()

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(args.wait)
        for selector in args.click:
            try:
                page.click(selector, timeout=5000)
                page.wait_for_timeout(3000)
                console.print(f"clicked {selector}")
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]click failed[/] {selector}: {exc}")
        Path(args.html).write_text(page.content(), encoding="utf-8")
        if args.text:
            Path(args.text).write_text(page.inner_text("body"), encoding="utf-8")
        console.print(f"[green]final url[/] {page.url}")
        browser.close()
    fh.close()
    console.print(f"[green]log[/] {out}")


if __name__ == "__main__":
    main()
