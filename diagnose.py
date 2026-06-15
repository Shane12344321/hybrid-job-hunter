"""
Diagnostic tool: shows EXACTLY what Playwright sees on each custom page.
Usage:
    python3 diagnose.py              # test all custom pages
    python3 diagnose.py "Google"     # test only pages matching "Google"
    python3 diagnose.py --screenshots  # save screenshots to ./screenshots/
"""
import yaml
import sys
import os
import time
import hashlib
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

SCREENSHOTS_DIR = "screenshots"

def diagnose_page(browser, page_config, keywords, save_screenshot=False):
    name = page_config["name"]
    url = page_config["url"]
    wait_for = page_config.get("wait_for_selector")
    css_selector = page_config.get("css_selector")

    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"  {url}")
    print(f"{'='*60}")

    try:
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=10000)
                print(f"  ✅ CSS selector '{wait_for}' loaded")
            except:
                print(f"  ⚠️  Timeout waiting for selector '{wait_for}'")

        time.sleep(3)

        if save_screenshot:
            os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            slug = name.lower().replace(" ", "_")
            path = os.path.join(SCREENSHOTS_DIR, f"{slug}.png")
            page.screenshot(path=path, full_page=False)
            print(f"  📸 Screenshot saved: {path}")

        content = page.content()
        page.close()

        soup = BeautifulSoup(content, "html.parser")

        if css_selector:
            elements = soup.select(css_selector)
            if elements:
                text = " ".join([el.get_text(separator=" ", strip=True) for el in elements])
                print(f"  CSS selector '{css_selector}': {len(elements)} element(s)")
            else:
                text = ""
                print(f"  ⚠️  CSS selector '{css_selector}' matched NOTHING")
        else:
            text = soup.body.get_text(separator=" ", strip=True) if soup.body else ""

        content_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
        empty_hash = hashlib.md5(b"").hexdigest()

        # Status
        if len(text) == 0 or content_hash == empty_hash:
            print(f"  ❌ EMPTY — Playwright got ZERO usable text")
            print(f"     This page is BROKEN. It's either blocking us or needs a different selector.")
            return False
        elif len(text) < 200:
            print(f"  ⚠️  SUSPICIOUS — Only {len(text)} chars (probably a CAPTCHA or error page)")
            print(f"     Text: {repr(text[:200])}")
            return False
        else:
            print(f"  ✅ Extracted {len(text):,} characters of text")

        # Keyword scan
        text_lower = text.lower()
        found = []
        not_found = []
        for kw in keywords:
            if kw in text_lower:
                found.append(kw)
            else:
                not_found.append(kw)

        if found:
            print(f"  ✅ Keywords FOUND: {found}")
        else:
            print(f"  ⚠️  No keywords matched")
        if not_found:
            print(f"     Keywords absent: {not_found}")

        # Show a snippet of what was extracted
        # Find the first occurrence of any keyword and show surrounding context
        snippet_shown = False
        for kw in found:
            idx = text_lower.find(kw)
            if idx >= 0:
                start = max(0, idx - 60)
                end = min(len(text), idx + len(kw) + 60)
                snippet = text[start:end]
                # Highlight the keyword
                print(f"\n  📝 Text snippet around '{kw}':")
                print(f"     \"...{snippet}...\"")
                snippet_shown = True
                break

        if not snippet_shown:
            print(f"\n  📝 First 200 chars of extracted text:")
            print(f"     \"{text[:200]}\"")

        print(f"\n  Hash: {content_hash}")
        return True

    except Exception as e:
        print(f"  ❌ CRASHED: {e}")
        return False


def main():
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    keywords = [k.lower() for k in config.get("keywords", [])]
    pages = config.get("custom_pages", [])

    save_screenshots = "--screenshots" in sys.argv
    filter_name = None
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            filter_name = arg.lower()

    if filter_name:
        pages = [p for p in pages if filter_name in p["name"].lower()]
        if not pages:
            print(f"No custom pages match '{filter_name}'")
            return

    print(f"🔬 PLAYWRIGHT DIAGNOSTIC — Testing {len(pages)} custom page(s)")
    print(f"   Keywords: {keywords}")
    if save_screenshots:
        print(f"   Screenshots: ON (saving to ./{SCREENSHOTS_DIR}/)")

    working = 0
    broken = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for page_config in pages:
            ok = diagnose_page(browser, page_config, keywords, save_screenshots)
            if ok:
                working += 1
            else:
                broken += 1
        browser.close()

    print(f"\n{'='*60}")
    print(f"  SUMMARY: {working} working, {broken} broken/suspicious out of {len(pages)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
