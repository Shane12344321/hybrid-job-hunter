import yaml
import json
import os
import sys
import hashlib
import html
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import re
import time
import calendar

STATE_FILE = "state.json"
TELEGRAM_MAX_LEN = 4096
# gzip only: some boards (e.g. OpenAI on Ashby) serve brotli, which trips a
# urllib3/brotlicffi streaming bug when a brotli package is installed.
API_HEADERS = {"Accept-Encoding": "gzip, deflate", "User-Agent": "Mozilla/5.0"}
FAILURE_ALERT_THRESHOLD = 3   # consecutive failed runs before a ⚠️ alert
MIN_PAGE_TEXT_CHARS = 200     # below this, a custom page is treated as blocked/broken


def _parse_ts(ts):
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0

class StateManager:
    """Tracks seen jobs/page hashes. Reserved keys (prefixed "_") hold
    non-company data such as the pending-alert queue."""

    def __init__(self):
        self.state = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    self.state = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.state = {}

    def _ensure(self, company):
        if company not in self.state:
            self.state[company] = {"jobs": [], "hash": ""}

    def is_new_job(self, company, job_id):
        self._ensure(company)
        return job_id not in self.state[company]["jobs"]

    def mark_job(self, company, job_id):
        self._ensure(company)
        if job_id not in self.state[company]["jobs"]:
            self.state[company]["jobs"].append(job_id)

    def hash_changed(self, company, content_hash):
        self._ensure(company)
        return content_hash != self.state[company].get("hash")

    def set_hash(self, company, content_hash):
        self._ensure(company)
        self.state[company]["hash"] = content_hash

    def queue_alert(self, message):
        self.state.setdefault("_pending", []).append({"message": message, "attempts": 0})

    def flush_pending(self, notifier, max_attempts=5):
        """Retry alerts that failed to deliver in previous runs."""
        pending = self.state.get("_pending", [])
        if not pending:
            return
        print(f"📬 Retrying {len(pending)} pending alert(s) from previous runs...")
        still_pending = []
        for entry in pending:
            entry["attempts"] += 1
            if notifier.send(entry["message"]):
                continue
            if entry["attempts"] >= max_attempts:
                print(f"Dropping alert after {entry['attempts']} failed delivery attempts.")
                continue
            still_pending.append(entry)
        self.state["_pending"] = still_pending

    def record_failure(self, source, reason):
        """Count a consecutive failure. The count stops growing once alerted,
        so a long-dead source doesn't mutate state (and trigger a commit)
        on every run."""
        failures = self.state.setdefault("_failures", {})
        entry = failures.setdefault(source, {"count": 0, "alerted": False})
        if not entry["alerted"]:
            entry["count"] += 1
            entry["reason"] = str(reason)[:200]
        return entry

    def record_success(self, source):
        """Clear a source's failure streak. Returns True if it had been
        alerted as failing (i.e. this is a recovery worth announcing)."""
        entry = self.state.get("_failures", {}).pop(source, None)
        return bool(entry and entry.get("alerted"))

    def sources_needing_alert(self):
        return {
            name: entry
            for name, entry in self.state.get("_failures", {}).items()
            if entry["count"] >= FAILURE_ALERT_THRESHOLD and not entry["alerted"]
        }

    def mark_failure_alerted(self, source):
        if source in self.state.get("_failures", {}):
            self.state["_failures"][source]["alerted"] = True

    def failing_now(self):
        return self.state.get("_failures", {})

    def record_new_jobs(self, count):
        """Keep a 7-day rolling log of finds (only written when something
        was found, so quiet runs don't churn state.json)."""
        if count <= 0:
            return
        stats = self.state.setdefault("_stats", {})
        finds = stats.setdefault("finds", [])
        finds.append([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), count])
        cutoff = time.time() - 7 * 86400
        stats["finds"] = [f for f in finds if _parse_ts(f[0]) >= cutoff]

    def new_jobs_since(self, seconds):
        cutoff = time.time() - seconds
        return sum(c for ts, c in self.state.get("_stats", {}).get("finds", []) if _parse_ts(ts) >= cutoff)

    def jobs_tracked(self):
        return sum(
            len(v.get("jobs", []))
            for k, v in self.state.items()
            if not k.startswith("_") and isinstance(v, dict)
        )

    def pending_count(self):
        return len(self.state.get("_pending", []))

    def save(self):
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)

class Notifier:
    def __init__(self, config):
        self.token = os.environ.get("TELEGRAM_TOKEN", config.get("telegram", {}).get("token", ""))
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", config.get("telegram", {}).get("chat_id", ""))
        self.enabled = bool(self.token and self.chat_id and not str(self.token).startswith("${"))

    def send(self, message, retries=3):
        """Returns True only when the message was actually delivered
        (or when running without credentials, where print is delivery)."""
        print(f"ALERT: {re.sub(r'<[^>]+>', '', message)}")
        if not self.enabled:
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(retries):
            try:
                res = requests.post(url, json=payload, timeout=15)
                if res.ok:
                    return True
                if res.status_code == 400:
                    # Malformed message — retrying won't help.
                    print(f"Telegram rejected message (400): {res.text[:200]}")
                    return False
                print(f"Telegram send failed ({res.status_code}), attempt {attempt + 1}/{retries}")
            except requests.RequestException as e:
                print(f"Telegram send error: {e} (attempt {attempt + 1}/{retries})")
            time.sleep(2 ** attempt)
        return False

def build_digest(new_jobs, page_changes, warnings=None):
    """Group all matches from one run into Telegram-sized message chunks.
    `warnings` are pre-escaped HTML lines (source failures/recoveries)."""
    lines = []
    if new_jobs:
        lines.append(f"🚀 <b>{len(new_jobs)} new role{'s' if len(new_jobs) != 1 else ''}</b>")
        by_company = {}
        for job in new_jobs:
            by_company.setdefault(job["company"], []).append(job)
        for company, jobs in by_company.items():
            lines.append("")
            lines.append(f"<b>{html.escape(company)}</b>")
            for job in jobs:
                title = html.escape(job.get("title") or "Untitled")
                location = html.escape(job.get("location") or "")
                job_url = html.escape(job.get("url") or "", quote=True)
                suffix = f" — {location}" if location else ""
                lines.append(f"• <a href='{job_url}'>{title}</a>{suffix}")
    for change in page_changes:
        lines.append("")
        page_url = html.escape(change["url"], quote=True)
        lines.append(f"🔍 <b>Changes detected at {html.escape(change['name'])}</b>")
        lines.append(f"Keywords & locations matched — <a href='{page_url}'>check the page</a>")
    for warning in warnings or []:
        lines.append("")
        lines.append(warning)

    chunks = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > TELEGRAM_MAX_LEN - 100:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks

def write_step_summary(run_report, new_jobs, state_manager):
    """Append a per-source result table to the GitHub Actions run summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Hybrid Hunter Run", "", "| Source | Result |", "|---|---|"]
    for name, status in run_report:
        safe_status = status.replace("|", "\\|")
        lines.append(f"| {name} | {safe_status} |")
    lines.append("")
    lines.append(
        f"**New roles this run:** {len(new_jobs)} · "
        f"**Pending alerts:** {state_manager.pending_count()} · "
        f"**Jobs tracked:** {state_manager.jobs_tracked()}"
    )
    try:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        print(f"Could not write step summary: {e}")

class ATSHunter:
    def __init__(self, config):
        self.keywords = [k.lower() for k in config.get("keywords", [])]
        self.exclude_keywords = [k.lower() for k in config.get("exclude_keywords") or []]
        self.locations = [l.lower() for l in config.get("locations", [])]

    def matches_criteria(self, title, location, keywords=None):
        """`keywords` overrides the global keyword list (per-company config).
        Titles hitting any exclude_keyword are rejected regardless."""
        if not title: return False
        title_lower = title.lower()
        location_lower = location.lower() if location else ""

        if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in self.exclude_keywords):
            return False

        keywords = self.keywords if keywords is None else keywords
        keyword_match = any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in keywords) if keywords else True
        location_match = any(l in location_lower for l in self.locations) if self.locations else True

        return keyword_match and location_match

    def title_matches(self, title, keywords=None):
        """Keyword/exclude check on the title only, ignoring location. Used by
        the Workday "N Locations" inclusion rule, where the location text is a
        count ("3 Locations") that can't match a city — so a title-only pass
        keeps India-eligible multi-location postings from being silently
        dropped (plan §4.1)."""
        if not title: return False
        title_lower = title.lower()
        if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in self.exclude_keywords):
            return False
        keywords = self.keywords if keywords is None else keywords
        return any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in keywords) if keywords else True

    # Hunters raise on failure (network error, non-200, bad JSON) so the
    # caller can tell a dead source apart from a source with no matches.

    def hunt_ashby(self, slug, keywords=None):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        res = requests.get(url, timeout=10, headers=API_HEADERS)
        res.raise_for_status()
        jobs = res.json().get("jobs", [])
        matches = []
        for job in jobs:
            title = job.get("title", "")
            location = job.get("location", "")
            if self.matches_criteria(title, location, keywords):
                matches.append({"id": job.get("id"), "title": title, "location": location, "url": job.get("jobUrl")})
        return matches

    def hunt_lever(self, slug, keywords=None):
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        res = requests.get(url, timeout=10, headers=API_HEADERS)
        res.raise_for_status()
        jobs = res.json()
        matches = []
        for job in jobs:
            title = job.get("text", "")
            location = job.get("categories", {}).get("location", "")
            if self.matches_criteria(title, location, keywords):
                matches.append({"id": job.get("id"), "title": title, "location": location, "url": job.get("hostedUrl")})
        return matches

    def hunt_greenhouse(self, slug, keywords=None):
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        res = requests.get(url, timeout=10, headers=API_HEADERS)
        res.raise_for_status()
        jobs = res.json().get("jobs", [])
        matches = []
        for job in jobs:
            title = job.get("title", "")
            location = job.get("location", {}).get("name", "")
            if self.matches_criteria(title, location, keywords):
                matches.append({"id": str(job.get("id")), "title": title, "location": location, "url": job.get("absolute_url")})
        return matches

    def hunt_oracle_hcm(self, site_number, keyword="intern", location_id=None,
                        host="jpmc.fa.oraclecloud.com", keywords=None):
        """Hunter for Oracle HCM Cloud portals. `host` selects the tenant
        (default JP Morgan); `keyword` is the server-side search term."""
        url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        params = f"onlyData=true&expand=requisitionList.secondaryLocations,requisitionList.requisitionFlexFields&finder=findReqs;siteNumber={site_number},keyword={keyword}"
        if location_id:
            params += f",locationId={location_id}"
        params += "&limit=25"
        res = requests.get(f"{url}?{params}", timeout=15, headers=API_HEADERS)
        res.raise_for_status()
        items = res.json().get("items", [])
        if not items:
            return []
        reqs = items[0].get("requisitionList", [])
        matches = []
        for job in reqs:
            title = job.get("Title", "")
            location = job.get("PrimaryLocation", "")
            if self.matches_criteria(title, location, keywords):
                job_id = str(job.get("Id", ""))
                job_url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site_number}/job/{job_id}"
                matches.append({"id": job_id, "title": title, "location": location, "url": job_url})
        return matches

    def hunt_workday(self, tenant, site, wd_host="wd5", search="intern",
                     include_multi_location=False, keywords=None):
        """Hunter for Workday CXS boards (NVIDIA, Citi, BlackRock, Adobe, …).
        POSTs to the tenant's CXS jobs endpoint with server-side `searchText`
        so one page is usually enough; paginates only up to the request cap.

        Location filtering is client-side on `locationsText` (via
        matches_criteria). We deliberately DON'T send Workday's location facet:
        the India GUID is tenant-specific (NVIDIA 400s on the shared one), so a
        hardcoded facet is brittle — searchText + matches_criteria is portable.
        `include_multi_location` gates the "N Locations" inclusion rule below."""
        base = f"https://{tenant}.{wd_host}.myworkdayjobs.com"
        url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        limit = 20
        matches = []
        # ≤ 4 requests/source: cap the pagination loop hard (plan §3).
        for page in range(4):
            body = {"appliedFacets": {}, "limit": limit, "offset": page * limit, "searchText": search}
            res = requests.post(url, json=body, timeout=15,
                                headers={**API_HEADERS, "Content-Type": "application/json", "Accept": "application/json"})
            res.raise_for_status()
            data = res.json()
            postings = data.get("jobPostings", [])
            for job in postings:
                title = job.get("title", "")
                location = job.get("locationsText", "")
                # Multi-location postings collapse to "N Locations" — that text
                # can't match a city, so matches_criteria would drop an
                # India-eligible role. With include_multi_location set, include
                # such a posting on a title match alone (one extra alert beats a
                # silent miss — see plan §4.1). Otherwise honour the location filter.
                is_multi = bool(re.match(r'^\d+\s+Locations$', location.strip()))
                if is_multi and include_multi_location:
                    if not self.title_matches(title, keywords):
                        continue
                elif not self.matches_criteria(title, location, keywords):
                    continue
                path = job.get("externalPath", "")
                # Stable dedup key: trailing requisition id (e.g. JR1988855),
                # falling back to the full path so IDs never come from position.
                job_id = path.rsplit("_", 1)[-1] if "_" in path else path
                job_url = f"{base}/en-US/{site}{path}"
                matches.append({"id": job_id, "title": title, "location": location, "url": job_url})
            total = data.get("total", 0)
            if (page + 1) * limit >= total or not postings:
                break
        return matches

    def hunt_amazon(self, query="intern", location="India", categories=None, keywords=None):
        """Hunter for amazon.jobs. One server-side filtered request is enough
        (Amazon's board is ~50k jobs, so query/loc params are mandatory).
        `loc_query` narrows server-side but still leaks nearby regions, so
        matches_criteria re-checks location client-side. `categories` narrows
        server-side via amazon.jobs' `category[]` param (live-verified:
        software-development / machine-learning-science / data-science) — without
        it Amazon India floods the digest with finance/ops interns."""
        url = (f"https://www.amazon.jobs/en/search.json?base_query={query}"
               f"&loc_query={location}&result_limit=100&offset=0")
        for c in categories or []:
            url += f"&category[]={c}"
        # A browser-like UA keeps the JSON endpoint from 403-ing (plan §4.2).
        res = requests.get(url, timeout=15, headers={
            **API_HEADERS,
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        res.raise_for_status()
        jobs = res.json().get("jobs", [])
        matches = []
        for job in jobs:
            title = job.get("title", "")
            location_text = job.get("normalized_location") or job.get("location", "")
            if self.matches_criteria(title, location_text, keywords):
                job_id = str(job.get("id_icims") or job.get("id", ""))
                job_url = f"https://www.amazon.jobs{job.get('job_path', '')}"
                matches.append({"id": job_id, "title": title, "location": location_text, "url": job_url})
        return matches

    def hunt_microsoft(self, query="intern", location="India", keywords=None):
        """Hunter for the Microsoft careers search API. Server-side `lc` filter
        narrows to a country; matches_criteria re-checks title/location.
        Paginates up to the request cap.

        NOTE: as of the last verification the gcsservices host serves an
        *.azureedge.net cert (hostname mismatch) and gates the search API
        behind a bearer token, so no MS entry ships in config.yaml yet. The
        adapter is kept ready for when the endpoint is reachable again — until
        then Phase 2 failure tracking would flag it, which is why it's
        unconfigured rather than special-cased."""
        matches = []
        for page in range(1, 5):  # pages 1-4, ≤ 4 requests (plan §3)
            url = (f"https://gcsservices.careers.microsoft.com/search/api/v1/search"
                   f"?q={query}&lc={location}&l=en_us&pg={page}&pgSz=20")
            res = requests.get(url, timeout=15, headers={**API_HEADERS, "Accept": "application/json"})
            res.raise_for_status()
            result = res.json().get("operationResult", {}).get("result", {})
            jobs = result.get("jobs", [])
            for job in jobs:
                title = job.get("title", "")
                locs = job.get("properties", {}).get("locations") or []
                location_text = ", ".join(locs) if isinstance(locs, list) else str(locs)
                if self.matches_criteria(title, location_text, keywords):
                    job_id = str(job.get("jobId", ""))
                    job_url = f"https://jobs.careers.microsoft.com/global/en/job/{job_id}"
                    matches.append({"id": job_id, "title": title, "location": location_text, "url": job_url})
            total = result.get("totalJobs", 0)
            if page * 20 >= total or not jobs:
                break
        return matches

def page_text_failure(text):
    """Returns a failure reason when extracted page text looks like a
    bot-block/CAPTCHA/error page rather than real content, else None.
    (Same heuristic as diagnose.py.)"""
    stripped = text.strip() if text else ""
    if len(stripped) == 0:
        return "empty page content — selector matched nothing or the page blocked us"
    if len(stripped) < MIN_PAGE_TEXT_CHARS:
        return f"suspicious page ({len(stripped)} chars) — possible CAPTCHA or error page"
    return None

class CustomWebHunter:
    def __init__(self, config):
        self.keywords = [k.lower() for k in config.get("keywords", [])]
        self.locations = [l.lower() for l in config.get("locations", [])]

    def hunt(self, page_config):
        url = page_config["url"]
        wait_for = page_config.get("wait_for_selector")
        css_selector = page_config.get("css_selector")
        keyword_filter = page_config.get("keyword_filter", False)
        location_filter = page_config.get("location_filter", True) # Default to true for safety
        
        try:
            with sync_playwright() as p:
                # Use Chromium for JS rendering
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                if wait_for:
                    try:
                        page.wait_for_selector(wait_for, timeout=10000)
                    except:
                        print(f"Timeout waiting for selector {wait_for} on {url}")
                
                # Give JS frameworks time to render dynamic content
                time.sleep(3)
                
                content = page.content()
                browser.close()
                
            soup = BeautifulSoup(content, "html.parser")
            
            if css_selector:
                elements = soup.select(css_selector)
                if not elements:
                    text_content = ""
                else:
                    text_content = " ".join([el.get_text(separator=' ', strip=True) for el in elements])
            else:
                # Fallback to whole body text
                text_content = soup.body.get_text(separator=' ', strip=True) if soup.body else ""

            failure = page_text_failure(text_content)
            if failure:
                return {"failed": failure}

            # Create a hash of the text content to detect visual changes
            content_hash = hashlib.md5(text_content.encode('utf-8')).hexdigest()
            
            matches_keywords = False
            matches_locations = False
            
            text_lower = text_content.lower()
            
            if keyword_filter and self.keywords:
                matches_keywords = any(re.search(r'\b' + re.escape(k) + r'\b', text_lower) for k in self.keywords)
            else:
                matches_keywords = True # Ignore keywords if filter is false
                
            if location_filter and self.locations:
                matches_locations = any(l in text_lower for l in self.locations)
            else:
                matches_locations = True
                
            is_match = matches_keywords and matches_locations
                
            return {
                "hash": content_hash,
                "matches_keywords": matches_keywords,
                "matches_locations": matches_locations,
                "is_match": is_match,
            }
        except Exception as e:
            return {"failed": f"page load crashed: {e}"}

def send_heartbeat(config, state_manager, notifier):
    """Read-only status report. Does NOT hunt and does NOT mutate state —
    the heartbeat workflow has no permission to commit state.json, so any
    hunting done here would be forgotten (duplicate or lost alerts)."""
    ats_count = len(config.get("ats_companies") or [])
    custom_count = len(config.get("custom_pages") or [])
    heartbeat = [
        "🤖 <b>Daily Heartbeat</b>",
        "",
        f"Sources: {ats_count} ATS board(s) + {custom_count} custom page(s)",
        f"New roles: {state_manager.new_jobs_since(86400)} in last 24h, "
        f"{state_manager.new_jobs_since(7 * 86400)} in last 7 days",
        f"Jobs tracked: {state_manager.jobs_tracked()} | Pending alerts: {state_manager.pending_count()}",
    ]
    failing = state_manager.failing_now()
    if failing:
        for name, entry in failing.items():
            streak = f"{entry['count']}+" if entry.get("alerted") else str(entry["count"])
            heartbeat.append(f"⚠️ {html.escape(name)}: failing ({streak} consecutive runs)")
    else:
        heartbeat.append("All sources healthy ✅")
    notifier.send("\n".join(heartbeat))

def main():
    test_mode = "--test" in sys.argv
    seed_mode = "--seed" in sys.argv
    hunt_ats = "--pages-only" not in sys.argv
    hunt_pages = "--ats-only" not in sys.argv

    if not os.path.exists("config.yaml"):
        print("config.yaml not found! Please create one based on the template.")
        return

    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    state_manager = StateManager()
    notifier = Notifier(config)
    ats_hunter = ATSHunter(config)
    custom_hunter = CustomWebHunter(config)

    if "--heartbeat" in sys.argv:
        send_heartbeat(config, state_manager, notifier)
        return

    if test_mode:
        print("="*60)
        print("🧪 TEST MODE — No state changes, no notifications")
        print(f"Keywords: {config.get('keywords', [])}")
        print(f"Locations: {config.get('locations', [])}")
        print("="*60)
    elif seed_mode:
        print("="*60)
        print("🌱 SEED MODE — Baselining state, no notifications")
        print("="*60)
    else:
        state_manager.flush_pending(notifier)

    new_jobs = []       # matches to notify, marked seen only after delivery
    page_changes = []   # custom-page changes, hash saved only after delivery
    run_report = []     # (source, status) per source, for the Actions summary
    recovered = []      # sources that came back after an alerted failure streak
    seen_this_run = set()
    seeded_jobs = 0
    seeded_pages = 0

    # 1. Process ATS Companies
    ats_companies = (config.get("ats_companies") or []) if hunt_ats else []
    if hunt_ats:
        print("\n📡 Hunting ATS boards...")
    else:
        print("\n📡 Skipping ATS boards (--pages-only)")
    for comp in ats_companies:
        name = comp["name"]
        ats_type = comp["ats"]
        slug = comp.get("slug", "")
        # Optional per-company keyword override (falls back to global keywords)
        kw_override = comp.get("keywords")
        if kw_override:
            kw_override = [k.lower() for k in kw_override]

        try:
            if ats_type == "ashby":
                matches = ats_hunter.hunt_ashby(slug, keywords=kw_override)
            elif ats_type == "lever":
                matches = ats_hunter.hunt_lever(slug, keywords=kw_override)
            elif ats_type == "greenhouse":
                matches = ats_hunter.hunt_greenhouse(slug, keywords=kw_override)
            elif ats_type == "oracle_hcm":
                matches = ats_hunter.hunt_oracle_hcm(
                    site_number=comp.get("site_number", "CX_1001"),
                    keyword=comp.get("keyword", "intern"),
                    location_id=comp.get("location_id"),
                    host=comp.get("host", "jpmc.fa.oraclecloud.com"),
                    keywords=kw_override
                )
            elif ats_type == "workday":
                matches = ats_hunter.hunt_workday(
                    tenant=comp["tenant"],
                    site=comp["site"],
                    wd_host=comp.get("wd_host", "wd5"),
                    search=comp.get("search", "intern"),
                    include_multi_location=comp.get("include_multi_location", False),
                    keywords=kw_override
                )
            elif ats_type == "amazon":
                matches = ats_hunter.hunt_amazon(
                    query=comp.get("query", "intern"),
                    location=comp.get("location", "India"),
                    categories=comp.get("categories"),
                    keywords=kw_override
                )
            elif ats_type == "microsoft":
                matches = ats_hunter.hunt_microsoft(
                    query=comp.get("query", "intern"),
                    location=comp.get("location", "India"),
                    keywords=kw_override
                )
            else:
                print(f"  ⚠️ {name}: unknown ats type '{ats_type}'")
                run_report.append((name, f"⚠️ unknown ats type '{ats_type}'"))
                continue
        except Exception as e:
            print(f"  ❌ {name} ({ats_type}): {e}")
            run_report.append((name, f"❌ {e}"))
            if not test_mode:
                state_manager.record_failure(name, e)
            continue

        run_report.append((name, f"✅ {len(matches)} match(es)"))
        if not test_mode and state_manager.record_success(name):
            recovered.append(name)

        if test_mode:
            print(f"  {'✅' if matches else '—'} {name} ({ats_type}): {len(matches)} matches")
            for m in matches[:3]:
                print(f"      → {m['title']} | {m['location']}")
            if len(matches) > 3:
                print(f"      ... and {len(matches) - 3} more")
            continue

        for match in matches:
            if not state_manager.is_new_job(name, match["id"]) or (name, match["id"]) in seen_this_run:
                continue
            seen_this_run.add((name, match["id"]))
            if seed_mode:
                state_manager.mark_job(name, match["id"])
                seeded_jobs += 1
            else:
                new_jobs.append({"company": name, **match})

    # 2. Process Custom Web Pages
    custom_pages = (config.get("custom_pages") or []) if hunt_pages else []
    if hunt_pages:
        print("\n🌐 Hunting Custom Web Pages (Playwright JS-rendered)...")
    else:
        print("\n🌐 Skipping custom pages (--ats-only)")
    for page_config in custom_pages:
        name = page_config["name"]
        url = page_config["url"]

        print(f"  Checking {name}...")
        result = custom_hunter.hunt(page_config)

        failed_reason = None
        if result is None:
            failed_reason = "page load failed"
        elif result.get("failed"):
            failed_reason = result["failed"]

        if failed_reason:
            print(f"  ❌ {name}: {failed_reason}")
            run_report.append((name, f"❌ {failed_reason}"))
            if not test_mode:
                state_manager.record_failure(name, failed_reason)
            continue

        if not test_mode and state_manager.record_success(name):
            recovered.append(name)

        if test_mode:
            status = "✅ MATCH" if result["is_match"] else "— NO match"
            details = f"(kw={result['matches_keywords']}, loc={result['matches_locations']})"
            print(f"  {status} {details} | hash={result['hash'][:12]}...")
            run_report.append((name, status))
            continue

        if not result["is_match"]:
            # Store the baseline hash without alerting when keywords/locations don't match.
            state_manager.set_hash(name, result["hash"])
            run_report.append((name, "— no keyword/location match"))
            continue
        if not state_manager.hash_changed(name, result["hash"]):
            run_report.append((name, "✅ no change"))
            continue
        run_report.append((name, "✅ CHANGED"))
        if seed_mode:
            state_manager.set_hash(name, result["hash"])
            seeded_pages += 1
        else:
            page_changes.append({"name": name, "url": url, "hash": result["hash"]})

    # 3. Deliver digest, THEN mark state. Undelivered chunks are queued in
    #    state.json and retried next run, so an alert is never silently lost.
    if not test_mode and not seed_mode:
        warnings = []
        for name in recovered:
            warnings.append(f"✅ <b>{html.escape(name)}</b> is healthy again.")
        for name, entry in state_manager.sources_needing_alert().items():
            warnings.append(
                f"⚠️ <b>{html.escape(name)}</b> has failed {entry['count']} runs in a row.\n"
                f"Last error: {html.escape(entry.get('reason', 'unknown'))}"
            )
            state_manager.mark_failure_alerted(name)

        if new_jobs or page_changes or warnings:
            for chunk in build_digest(new_jobs, page_changes, warnings):
                if not notifier.send(chunk):
                    state_manager.queue_alert(chunk)
            for job in new_jobs:
                state_manager.mark_job(job["company"], job["id"])
            for change in page_changes:
                state_manager.set_hash(change["name"], change["hash"])
            state_manager.record_new_jobs(len(new_jobs))

    if not test_mode:
        state_manager.save()
    write_step_summary(run_report, new_jobs, state_manager)

    print("\n✅ Done!")
    if seed_mode:
        print(f"🌱 Seeded {seeded_jobs} job(s) and {seeded_pages} page baseline(s) into {STATE_FILE}")

if __name__ == "__main__":
    main()
