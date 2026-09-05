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
import re
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

SCREENSHOTS_DIR = "screenshots"


def suggest_selectors(html, keywords=None, limit=5):
    """Return the top repeated job-card selectors (compatibility helper)."""
    return [item["job_selector"] for item in suggest_selector_snippets(
        html, keywords, limit=limit)]


def suggest_selector_snippets(html, keywords=None, limit=5):
    """Suggest ready-to-paste selectors from repeated rendered structures."""
    soup = BeautifulSoup(html, "html.parser")
    keywords = [str(value).casefold() for value in (keywords or [])]
    groups = {}
    for element in soup.find_all(True):
        classes = tuple(value for value in (element.get("class") or [])
                        if value and all(ch.isalnum() or ch in "_-" for ch in value))
        anchors = element.select("a[href]")
        if not classes or not anchors:
            continue
        key = (element.name, classes)
        bucket = groups.setdefault(key, [])
        bucket.append(element)

    ranked = []
    for (tag, classes), members in groups.items():
        if len(members) < 3:
            continue
        keyword_members = [member for member in members if any(
            keyword in member.get_text(" ", strip=True).casefold() for keyword in keywords)]
        if not keyword_members:
            continue
        job_selector = tag + "".join(f".{value}" for value in classes)
        anchors = [member.select_one("a[href]") for member in members]
        hrefs = [anchor.get("href", "") for anchor in anchors if anchor]
        title_selector = None
        title_member = keyword_members[0]
        anchor = title_member.select_one("a[href]")
        anchor_text = anchor.get_text(" ", strip=True) if anchor else ""
        if anchor_text:
            matching = []
            for child in title_member.find_all(True):
                if anchor_text.casefold() in child.get_text(" ", strip=True).casefold():
                    matching.append(child)
            if matching:
                deepest = max(matching, key=lambda child: len(list(child.parents)))
                title_selector = deepest.name + "".join(
                    f".{value}" for value in (deepest.get("class") or [])
                    if all(ch.isalnum() or ch in "_-" for ch in value))
        title_selector = title_selector or "a"
        id_regex = _common_id_regex(hrefs)
        ranked.append({
            "job_selector": job_selector,
            "title_selector": title_selector,
            "id_regex": id_regex,
            "keyword_members": len(keyword_members),
            "total_members": len(members),
        })
    ranked.sort(key=lambda item: (-item["keyword_members"], -item["total_members"],
                                  item["job_selector"]))
    return ranked[:limit]


def _common_id_regex(hrefs):
    numeric = [href for href in hrefs if re.search(r"(?:^|/)\d+(?:[/?#]|$)", href)]
    if len(numeric) >= 2:
        return r"/(\d+)(?:[/?#]|$)"
    uuid = [href for href in hrefs if re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        href, re.I)]
    if len(uuid) >= 2:
        return r"/([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})(?:[/?#]|$)"
    return None


def diagnose_page(browser, page_config, keywords, save_screenshot=False,
                  suggest=False):
    name = page_config["name"]
    url = page_config["url"]
    wait_for = page_config.get("wait_for_selector")
    css_selector = page_config.get("css_selector")
    wait_timed_out = False

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
        response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if response is None:
            raise RuntimeError("navigation returned no HTTP response")
        if not response.ok:
            raise RuntimeError(f"page returned HTTP {response.status}")

        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=10000)
                print(f"  ✅ CSS selector '{wait_for}' loaded")
            except Exception:
                wait_timed_out = True
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
                if suggest:
                    suggestions = suggest_selector_snippets(content, keywords)
                    if suggestions:
                        print("  💡 Selector suggestions (human verification required):")
                        for suggestion in suggestions:
                            print(yaml.safe_dump({key: suggestion[key] for key in (
                                "job_selector", "title_selector", "id_regex")
                                if suggestion.get(key) is not None},
                                sort_keys=False, default_flow_style=False).rstrip())
        else:
            text = soup.body.get_text(separator=" ", strip=True) if soup.body else ""
            if suggest:
                suggestions = suggest_selector_snippets(content, keywords)
                if suggestions:
                    print("  💡 Selector suggestions (human verification required):")
                    for suggestion in suggestions:
                        print(yaml.safe_dump({key: suggestion[key] for key in (
                            "job_selector", "title_selector", "id_regex")
                            if suggestion.get(key) is not None},
                            sort_keys=False, default_flow_style=False).rstrip())

        if wait_timed_out:
            zero_markers = page_config.get("zero_result_text") or []
            if isinstance(zero_markers, str):
                zero_markers = [zero_markers]
            if not any(marker.lower() in text.lower() for marker in zero_markers):
                print("  ❌ Configured wait selector timed out without an explicit zero-result marker")
                return False

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
        elif any(marker in text.lower() for marker in (
                "access denied", "verify you are human", "captcha", "just a moment",
                "service unavailable", "internal server error", "error 404", "page not found")):
            print("  ❌ ERROR/BLOCK PAGE — recognized an HTTP error or bot-block marker")
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
    suggest = "--suggest-selectors" in sys.argv
    filter_name = None
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            filter_name = arg.lower()

    if filter_name:
        pages = [p for p in pages if filter_name in p["name"].lower()]
        if not pages:
            print(f"No custom pages match '{filter_name}'")
            return 1

    print(f"🔬 PLAYWRIGHT DIAGNOSTIC — Testing {len(pages)} custom page(s)")
    print(f"   Keywords: {keywords}")
    if save_screenshots:
        print(f"   Screenshots: ON (saving to ./{SCREENSHOTS_DIR}/)")
    if suggest:
        print("   Selector suggestions: ON")

    working = 0
    broken = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for page_config in pages:
            ok = diagnose_page(browser, page_config, keywords, save_screenshots, suggest)
            if ok:
                working += 1
            else:
                broken += 1
        browser.close()

    print(f"\n{'='*60}")
    print(f"  SUMMARY: {working} working, {broken} broken/suspicious out of {len(pages)}")
    print(f"{'='*60}")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
