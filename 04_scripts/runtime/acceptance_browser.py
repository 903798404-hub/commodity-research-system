"""Generic real Chromium DOM acceptance. Requires pre-provisioned Playwright.

Read-only browser navigation only, loopback endpoint supplied by the collector.
Missing browser/dependency is FAIL, never skip or HTTP-only success.
"""
import json
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit


def inspect_page(page, endpoint, config):
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
        raise ValueError("candidate browser endpoint must be loopback")
    path = config["path"]
    if not path.startswith("/?") or "\\" in path or "#" in path:
        raise ValueError("invalid acceptance route")
    page.goto(endpoint + path, wait_until="domcontentloaded", timeout=60000)
    tables = page.locator(config["table_selector"])
    tables.nth(len(config["headings"]) - 1).wait_for(state="visible", timeout=60000)
    if tables.count() != len(config["headings"]):
        raise ValueError("unexpected table count")
    observations = []
    for index, heading in enumerate(config["headings"]):
        table = tables.nth(index)
        section = table.locator("xpath=ancestor::section[1]")
        if section.locator("h2").inner_text().strip() != heading:
            raise ValueError("table session heading differs")
        rows = table.locator("tbody tr")
        if rows.count() != config["rows"]:
            raise ValueError("month row count differs")
        months = rows.locator(config["month_selector"]).all_text_contents()
        if [v.strip()[-2:] for v in months] != config["month_sequence"]:
            raise ValueError("month sequence differs")
        for selector in config["empty_value_selectors"]:
            values = rows.locator(selector).all_text_contents()
            if len(values) != config["rows"] or any(v.strip() not in config["empty_tokens"] for v in values):
                raise ValueError("empty candidate store contains fabricated business values")
        observations.append(dict(heading=heading, rows=rows.count(), months=months, html=table.inner_html()))
    if page.locator('[data-testid="stException"]').count():
        raise ValueError("Streamlit exception")
    return observations


def main():
    request = json.load(sys.stdin)
    result = dict(identity=request["identity"], context=request["context"],
                  timestamp=datetime.now(timezone.utc).isoformat(), status="FAIL", observations=[])
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                endpoint = request["endpoint"]
                # Prevent the read-only acceptance browser from contacting providers.
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(endpoint + "/") else route.abort())
                result["observations"] = inspect_page(page, endpoint, request["config"])
                result["status"] = "PASS"
            finally:
                browser.close()
    except Exception as exc:
        result["observations"] = [{"error_type": type(exc).__name__}]
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
