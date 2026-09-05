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
import uuid
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

STATE_FILE = "state.json"
TELEGRAM_MAX_LEN = 4096
# gzip only: some boards (e.g. OpenAI on Ashby) serve brotli, which trips a
# urllib3/brotlicffi streaming bug when a brotli package is installed.
API_HEADERS = {"Accept-Encoding": "gzip, deflate", "User-Agent": "Mozilla/5.0"}
FAILURE_ALERT_THRESHOLD = 3   # consecutive failed runs before a ⚠️ alert
FAILING_SOURCE_RETRY_SECONDS = 6 * 60 * 60
MIN_PAGE_TEXT_CHARS = 200     # below this, a custom page is treated as blocked/broken
HTTP_RETRY_ATTEMPTS = 3       # initial request plus two retries
HTTP_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
HTTP_RETRY_AFTER_CAP = 20
sleep = time.sleep             # patchable in offline tests
DEFAULT_CONCURRENCY = 8
MAX_CONCURRENCY = 32


def _parse_ts(ts):
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0

class StateManager:
    """Tracks seen jobs/page hashes. Reserved keys (prefixed "_") hold
    non-company data such as the pending-alert queue."""

    def __init__(self, state_file=None):
        self.path = state_file or STATE_FILE
        self.state = {}
        self.dirty = False
        self._job_sets = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("top-level state must be a JSON object")
                self._validate_shape(loaded)
                self.state = loaded
                self._job_sets = {
                    key: set(value["jobs"])
                    for key, value in loaded.items() if not key.startswith("_")
                }
            except (json.JSONDecodeError, OSError, ValueError) as e:
                raise RuntimeError(
                    f"Could not load {self.path}; refusing to discard crawler state: {e}") from e

    @staticmethod
    def _validate_shape(state):
        """Fail closed on structural drift. A malformed bucket would otherwise
        be silently rewritten (data loss) or crash mid-run after alerts were
        marked delivered (alert loss)."""
        reserved_types = {
            "_pending": list, "_failures": dict, "_health": dict, "_stats": dict,
        }
        for bucket, expected in reserved_types.items():
            if bucket in state and not isinstance(state[bucket], expected):
                raise ValueError(
                    f"state bucket '{bucket}' must be a {expected.__name__}")
        seen_keys = {}
        for key, value in state.items():
            if key.startswith("_"):
                continue
            if not isinstance(value, dict):
                raise ValueError(f"source '{key}' must be a JSON object")
            jobs = value.get("jobs")
            if not isinstance(jobs, list):
                raise ValueError(
                    f"source '{key}' has malformed 'jobs' (must be a list)")
            folded = key.casefold()
            if folded in seen_keys:
                raise ValueError(
                    f"ambiguous case-insensitive source keys "
                    f"'{seen_keys[folded]}' and '{key}' collide in state")
            seen_keys[folded] = key

    def _resolve(self, company):
        """Map a source name onto its stored key, case-insensitively, so a
        case-only rename (Microsoft -> MICROSOFT) keeps its history instead of
        replaying the board. Exact and casefolded lookups."""
        if company in self.state:
            return company
        folded = company.casefold()
        for key in self.state:
            if not key.startswith("_") and key.casefold() == folded:
                return key
        return company

    def _ensure(self, company):
        company = self._resolve(company)
        if company not in self.state:
            self.state[company] = {"jobs": [], "hash": ""}
            self._job_sets[company] = set()
            self.dirty = True
        return company

    def is_new_job(self, company, job_id):
        company = self._ensure(company)
        return job_id not in self._job_sets[company]

    def mark_job(self, company, job_id):
        company = self._ensure(company)
        entry = self.state[company]
        if job_id not in self._job_sets[company]:
            entry["jobs"].append(job_id)
            self._job_sets[company].add(job_id)
            self.dirty = True
            # First-seen timestamp enables expiry pruning (prune_expired_jobs)
            # without changing the jobs list's shape or existing consumers.
            entry.setdefault("seen", {}).setdefault(str(job_id), int(time.time()))

    def hash_changed(self, company, content_hash):
        company = self._ensure(company)
        return content_hash != self.state[company].get("hash")

    def set_hash(self, company, content_hash):
        company = self._ensure(company)
        if self.state[company].get("hash") != content_hash:
            self.state[company]["hash"] = content_hash
            self.dirty = True

    def queue_alert(self, message):
        self.state.setdefault("_pending", []).append({"message": message, "attempts": 0})
        self.dirty = True

    def flush_pending(self, notifier, max_attempts=5):
        """Retry alerts that failed to deliver in previous runs."""
        pending = self.state.get("_pending", [])
        if not pending:
            return
        self.dirty = True
        print(f"📬 Retrying {len(pending)} pending alert(s) from previous runs...")
        still_pending = []
        for entry in pending:
            entry["attempts"] = min(int(entry.get("attempts", 0)) + 1, max_attempts)
            if notifier.send(entry["message"]):
                continue
            if entry["attempts"] >= max_attempts:
                print(f"Alert still undelivered after {entry['attempts']} attempts; retaining it for future runs.")
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
            self.dirty = True
        entry["last_failure"] = time.time()
        self.dirty = True
        return entry

    def source_in_backoff(self, source, now=None):
        """Whether an alerted source should wait before another probe."""
        entry = self.state.get("_failures", {}).get(source)
        if not entry or not entry.get("alerted"):
            return False
        last_failure = entry.get("last_failure")
        if last_failure is None:
            return False
        try:
            return (time.time() if now is None else now) - float(last_failure) \
                < FAILING_SOURCE_RETRY_SECONDS
        except (TypeError, ValueError):
            return False

    def record_success(self, source):
        """Clear a source's failure streak. Returns True if it had been
        alerted as failing (i.e. this is a recovery worth announcing)."""
        entry = self.state.get("_failures", {}).pop(source, None)
        if entry is not None:
            self.dirty = True
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
            self.dirty = True

    def failing_now(self):
        return self.state.get("_failures", {})

    def prune_removed_sources(self, configured_names):
        """Drop failure/health rows for sources no longer in config.yaml.

        record_success only fires when a source actually runs, so a source
        deleted from config while failing would otherwise stay in `_failures`
        forever — reported as a live failure by every heartbeat and catalog
        report. Membership comes from the whole config, not from what this run
        hunted, so shard and --company runs never prune each other's rows."""
        keep = {name.casefold() for name in configured_names}
        removed = []
        for bucket in ("_failures", "_health"):
            rows = self.state.get(bucket)
            if not isinstance(rows, dict):
                continue
            for name in [n for n in rows if n.casefold() not in keep]:
                del rows[name]
                removed.append(name)
                self.dirty = True
            if not rows:
                self.state.pop(bucket, None)
        return sorted(set(removed))

    def prune_expired_jobs(self, current_raw_results=None, days=60):
        """Prune old seen ids that disappeared from the current raw boards.

        ``current_raw_results`` maps source names to the ids observed in this
        run before filtering. Legacy ids without a timestamp are stamped now
        on first sight and are never removed by this pass.
        """
        if not isinstance(days, (int, float)) or days < 1:
            raise ValueError("days must be positive")
        current_raw_results = current_raw_results or {}
        normalized = {
            str(name).casefold(): {str(job_id) for job_id in ids}
            for name, ids in current_raw_results.items()
        }
        cutoff = time.time() - days * 86400
        removed = []
        now = int(time.time())
        for source, entry in self.state.items():
            if source.startswith("_") or not isinstance(entry, dict):
                continue
            seen = entry.setdefault("seen", {})
            current = normalized.get(source.casefold(), set())
            kept_jobs = []
            for job_id in entry.get("jobs", []):
                key = str(job_id)
                timestamp = seen.get(key)
                if timestamp is None:
                    # Legacy state has no age evidence. Stamp it when the id
                    # is observed, but never infer that it is safe to delete.
                    if key in current:
                        seen[key] = now
                    kept_jobs.append(job_id)
                    continue
                try:
                    old = float(timestamp)
                except (TypeError, ValueError):
                    kept_jobs.append(job_id)
                    continue
                if old < cutoff and key not in current:
                    removed.append((source, job_id))
                    continue
                kept_jobs.append(job_id)
            if len(kept_jobs) != len(entry.get("jobs", [])):
                entry["jobs"] = kept_jobs
                self._job_sets[source] = set(kept_jobs)
                self.dirty = True
            if not seen:
                entry.pop("seen", None)
        return removed

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
        self.dirty = True

    def record_source_run(self, source, source_type, success, match_count,
                          duration_seconds, request_count, reason=None,
                          raw_count=None):
        """Persist compact source-health evidence and detect silent collapse."""
        health = self.state.setdefault("_health", {})
        previous = health.get(source) or {}
        zero_streak = previous.get("successful_zero_streak", 0)
        if success:
            zero_streak = zero_streak + 1 if match_count == 0 else 0
        entry = {
            "source_type": source_type,
            "last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "success": bool(success),
            "last_match_count": match_count if success else None,
            "raw_count": raw_count if success else None,
            "successful_zero_streak": zero_streak,
            "duration_ms": max(0, int(duration_seconds * 1000)),
            "request_count": max(0, int(request_count)),
        }
        if success:
            entry["last_success"] = entry["last_run"]
            if previous.get("last_failure"):
                entry["last_failure"] = previous["last_failure"]
        else:
            entry["last_failure"] = entry["last_run"]
            entry["last_success"] = previous.get("last_success")
            entry["reason"] = str(reason)[:200]
        if success:
            previous_matches = previous.get("last_match_count")
            previous_raw = previous.get("raw_count")
            suspect = None
            if (isinstance(previous_matches, int) and previous_matches >= 5
                    and match_count == 0):
                suspect = (
                    f"filtered matches collapsed from {previous_matches} to zero")
            elif (isinstance(previous_raw, int) and previous_raw >= 20
                  and isinstance(raw_count, int)
                  and raw_count < previous_raw * 0.2):
                suspect = (
                    f"raw postings collapsed from {previous_raw} to {raw_count}")
            if suspect:
                entry["suspect"] = suspect
        health[source] = entry
        self.dirty = True

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
        target = os.path.abspath(self.path)
        directory = os.path.dirname(target)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    "w", dir=directory, prefix=f".{os.path.basename(target)}.",
                    suffix=".tmp", delete=False) as f:
                temp_path = f.name
                json.dump(self.state, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, target)
            self.dirty = False
        except Exception:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise

    def save_if_dirty(self):
        """Checkpoint state only when a mutating operation changed it."""
        if self.dirty:
            self.save()

class Notifier:
    def __init__(self, config):
        telegram = config.get("telegram") or {}
        self.token = os.environ.get("TELEGRAM_TOKEN", telegram.get("token", ""))
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", telegram.get("chat_id", ""))
        token = str(self.token).strip()
        chat_id = str(self.chat_id).strip()
        self.enabled = bool(
            token and chat_id and not token.startswith("${") and not chat_id.startswith("${")
        )

    def send(self, message, retries=3):
        """Returns True only when the message was actually delivered
        to Telegram."""
        print(f"ALERT: {re.sub(r'<[^>]+>', '', message)}")
        if not self.enabled:
            print("Telegram delivery is disabled because credentials are missing or unresolved.")
            return False
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
                    try:
                        response_body = res.json()
                    except ValueError:
                        response_body = None
                    if isinstance(response_body, dict) and response_body.get("ok") is True:
                        return True
                    print("Telegram returned a success status without an ok=true response body.")
                    return False
                if res.status_code == 400:
                    # Malformed message — retrying won't help.
                    print(f"Telegram rejected message (400): {res.text[:200]}")
                    return False
                print(f"Telegram send failed ({res.status_code}), attempt {attempt + 1}/{retries}")
            except requests.RequestException as e:
                print(f"Telegram send error: {e} (attempt {attempt + 1}/{retries})")
            time.sleep(2 ** attempt)
        return False

def _clip(value, limit):
    text = str(value or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


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
            lines.append(f"<b>{html.escape(_clip(company, 300))}</b>")
            for job in jobs:
                title = html.escape(_clip(job.get("title") or "Untitled", 1000))
                location = html.escape(_clip(job.get("location") or "", 400))
                raw_url = str(job.get("url") or "")
                suffix = f" — {location}" if location else ""
                if raw_url and len(raw_url) <= 1800:
                    job_url = html.escape(raw_url, quote=True)
                    lines.append(f"• <a href='{job_url}'>{title}</a>{suffix}")
                else:
                    lines.append(f"• {title}{suffix} (link unavailable)")
    for change in page_changes:
        lines.append("")
        raw_url = str(change["url"])
        lines.append(f"🔍 <b>Changes detected at {html.escape(_clip(change['name'], 300))}</b>")
        if len(raw_url) <= 1800:
            page_url = html.escape(raw_url, quote=True)
            lines.append(f"Keywords & locations matched — <a href='{page_url}'>check the page</a>")
        else:
            lines.append("Keywords & locations matched — page URL is too long to include safely")
    for warning in warnings or []:
        lines.append("")
        lines.append(warning)

    # Every generated line is bounded above. Keep a defensive plain-text
    # fallback so an unexpected oversized warning cannot create a Telegram 400
    # that is retried forever.
    bounded_lines = []
    max_chunk = TELEGRAM_MAX_LEN - 100
    for line in lines:
        if len(line) <= max_chunk:
            bounded_lines.append(line)
            continue
        plain = re.sub(r"<[^>]+>", "", line)
        for start in range(0, len(plain), 600):
            bounded_lines.append(html.escape(plain[start:start + 600]))

    chunks = []
    current = ""
    for line in bounded_lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_chunk:
            if current:
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
        self.exclude_locations = [l.lower() for l in config.get("exclude_locations") or []]
        self.request_count = 0
        self.last_raw_count = None
        self.last_raw_ids = set()

    def reset_request_count(self):
        self.request_count = 0
        self.last_raw_count = None
        self.last_raw_ids = set()

    def _get(self, *args, **kwargs):
        return self._request(requests.get, *args, **kwargs)

    def _post(self, *args, **kwargs):
        return self._request(requests.post, *args, **kwargs)

    def _request(self, method, *args, **kwargs):
        """Issue a request with bounded retries for transient failures.

        Every attempt increments ``request_count`` because pagination budgets
        describe actual network requests, not logical adapter calls. HTTP
        responses are returned unchanged after the final attempt so each
        adapter's existing ``raise_for_status`` path remains authoritative.
        """
        last_attempt = HTTP_RETRY_ATTEMPTS - 1
        for attempt in range(HTTP_RETRY_ATTEMPTS):
            self.request_count += 1
            try:
                response = method(*args, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= last_attempt:
                    raise
                sleep(min(2 ** (attempt + 1), 8))
                continue

            status = getattr(response, "status_code", None)
            if status not in HTTP_RETRY_STATUS_CODES:
                return response
            if attempt >= last_attempt:
                # Preserve the existing adapter failure path for the final
                # transient HTTP response, while returning non-error responses
                # untouched for callers that inspect them directly.
                response.raise_for_status()
                return response
            retry_after = getattr(response, "headers", {}).get("Retry-After")
            try:
                delay = min(float(retry_after), HTTP_RETRY_AFTER_CAP)
                if delay < 0:
                    raise ValueError
            except (TypeError, ValueError):
                delay = min(2 ** (attempt + 1), 8)
            sleep(delay)
        raise AssertionError("unreachable retry loop")

    @staticmethod
    def _title_for_matching(title):
        # `_` is often a visual separator, while dots inside abbreviations
        # should not let "Ph.D." evade an explicit `phd` exclusion.
        normalized = title.lower().replace("_", " ")
        return re.sub(r"(?<=\w)\.(?=\w)", "", normalized)

    def matches_criteria(self, title, location, keywords=None):
        """`keywords` overrides the global keyword list (per-company config).
        Titles hitting any exclude_keyword are rejected regardless."""
        if not title: return False
        title_lower = self._title_for_matching(title)

        if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in self.exclude_keywords):
            return False

        keywords = self.keywords if keywords is None else keywords
        keyword_match = any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in keywords) if keywords else True
        return keyword_match and self.location_matches(location)

    def location_matches(self, location):
        """Word-boundary location check, minus any `exclude_locations` hit.

        Word boundaries matter because a bare substring test lets "india" match
        "Indiana"/"Indianapolis". `exclude_locations` exists because "remote" has
        to stay in `locations` to catch remote India roles, but on its own it
        also admits "Remote - USA"; listing the foreign markers is the only way
        to keep one without the other."""
        location_lower = location.lower() if location else ""
        if any(re.search(r'\b' + re.escape(x) + r'\b', location_lower)
               for x in self.exclude_locations):
            return False
        if not self.locations:
            return True
        return any(re.search(r'\b' + re.escape(l) + r'\b', location_lower)
                   for l in self.locations)

    def title_matches(self, title, keywords=None):
        """Keyword/exclude check on the title only, ignoring location. Used by
        the Workday "N Locations" inclusion rule, where the location text is a
        count ("3 Locations") that can't match a city — so a title-only pass
        keeps India-eligible multi-location postings from being silently
        dropped (plan §4.1)."""
        if not title: return False
        title_lower = self._title_for_matching(title)
        if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in self.exclude_keywords):
            return False
        keywords = self.keywords if keywords is None else keywords
        return any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in keywords) if keywords else True

    # Hunters raise on failure (network error, non-200, bad JSON) so the
    # caller can tell a dead source apart from a source with no matches.

    def hunt_ashby(self, slug, keywords=None):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        res = self._get(url, timeout=10, headers=API_HEADERS)
        res.raise_for_status()
        payload = res.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Ashby response missing jobs[]")
        jobs = payload["jobs"]
        self.last_raw_count = len(jobs)
        raw_ids = set()
        matches = []
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError("Ashby jobs[] contained a non-object entry")
            job_id = str(job.get("id") or "")
            raw_ids.add(job_id)
            title = job.get("title")
            location = job.get("location")
            job_url = job.get("jobUrl")
            if (not job_id or not isinstance(title, str) or not title.strip()
                    or not isinstance(location, str) or not isinstance(job_url, str) or not job_url):
                raise ValueError("Ashby job missing id/title/location/jobUrl")
            if self.matches_criteria(title, location, keywords):
                matches.append({"id": job_id, "title": title, "location": location, "url": job_url})
        self.last_raw_ids = raw_ids
        return matches

    def hunt_lever(self, slug, keywords=None):
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        res = self._get(url, timeout=10, headers=API_HEADERS)
        res.raise_for_status()
        jobs = res.json()
        if not isinstance(jobs, list):
            raise ValueError("Lever response is not a postings array")
        self.last_raw_count = len(jobs)
        raw_ids = set()
        matches = []
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("categories"), dict):
                raise ValueError("Lever posting missing categories object")
            job_id = str(job.get("id") or "")
            raw_ids.add(job_id)
            title = job.get("text")
            location = job["categories"].get("location")
            job_url = job.get("hostedUrl")
            if (not job_id or not isinstance(title, str) or not title.strip()
                    or not isinstance(location, str) or not isinstance(job_url, str) or not job_url):
                raise ValueError("Lever posting missing id/text/location/hostedUrl")
            if self.matches_criteria(title, location, keywords):
                matches.append({"id": job_id, "title": title, "location": location, "url": job_url})
        self.last_raw_ids = raw_ids
        return matches

    def hunt_greenhouse(self, slug, keywords=None):
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        res = self._get(url, timeout=10, headers=API_HEADERS)
        res.raise_for_status()
        payload = res.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Greenhouse response missing jobs[]")
        jobs = payload["jobs"]
        self.last_raw_count = len(jobs)
        raw_ids = set()
        matches = []
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("location"), dict):
                raise ValueError("Greenhouse job missing location object")
            job_id = str(job.get("id") or "")
            raw_ids.add(job_id)
            title = job.get("title")
            location = job["location"].get("name")
            job_url = job.get("absolute_url")
            if (not job_id or not isinstance(title, str) or not title.strip()
                    or not isinstance(location, str) or not isinstance(job_url, str) or not job_url):
                raise ValueError("Greenhouse job missing id/title/location/absolute_url")
            if self.matches_criteria(title, location, keywords):
                matches.append({"id": job_id, "title": title, "location": location, "url": job_url})
        self.last_raw_ids = raw_ids
        return matches

    def hunt_smartrecruiters(self, company_id, country=None, query=None,
                             queries=None, keywords=None):
        """Hunt SmartRecruiters' public Posting API within four requests."""
        endpoint = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
        limit = 100
        matches = []
        seen_ids = set()
        raw_count = 0
        search_terms = queries if queries is not None else [query]
        if not search_terms:
            search_terms = [None]
        requests_used = 0
        for term in search_terms:
            offset = 0
            expected_total = None
            query_seen_ids = set()
            while expected_total is None or offset < expected_total:
                if requests_used >= 4:
                    label = term or "all postings"
                    raise RuntimeError(
                        f"SmartRecruiters pagination truncated for '{label}' "
                        f"at {offset}/{expected_total} after 4 shared requests")
                params = {"limit": limit, "offset": offset}
                if country:
                    params["country"] = country
                if term:
                    params["q"] = term
                res = self._get(endpoint, params=params, timeout=15, headers=API_HEADERS)
                requests_used += 1
                res.raise_for_status()
                payload = res.json()
                if (not isinstance(payload, dict)
                        or not isinstance(payload.get("content"), list)
                        or "totalFound" not in payload):
                    raise ValueError("SmartRecruiters response missing totalFound/content[]")
                try:
                    total = int(payload["totalFound"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("SmartRecruiters totalFound is not an integer") from exc
                # Trust the first page's total only. Some tenants report a
                # reset total on later offsets; shortening the read here would
                # make a partial page look complete.
                if expected_total is None:
                    expected_total = total
                postings = payload["content"]
                raw_count += len(postings)
                for posting in postings:
                    if (not isinstance(posting, dict)
                            or not isinstance(posting.get("location"), dict)):
                        raise ValueError("SmartRecruiters posting missing location object")
                    job_id = str(posting.get("id") or posting.get("uuid") or "")
                    title = posting.get("name")
                    location = posting["location"]
                    location_text = location.get("fullLocation") or ", ".join(
                        str(location.get(field)).strip()
                        for field in ("city", "region", "country")
                        if location.get(field))
                    if location.get("remote"):
                        location_text = (
                            f"{location_text}, Remote" if location_text else "Remote")
                    if (not job_id or not isinstance(title, str) or not title.strip()
                            or not location_text):
                        raise ValueError("SmartRecruiters posting missing id/name/location")
                    # A repeated id inside one query's pages means broken
                    # pagination (overlap), not a legitimate duplicate.
                    if job_id in query_seen_ids:
                        raise RuntimeError(
                            f"SmartRecruiters pagination repeated posting {job_id}; "
                            "refusing an incomplete read")
                    query_seen_ids.add(job_id)
                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)
                    if self.matches_criteria(title, location_text, keywords):
                        matches.append({
                            "id": job_id,
                            "title": title,
                            "location": location_text,
                            "url": f"https://jobs.smartrecruiters.com/{company_id}/{job_id}",
                        })
                offset += len(postings)
                if not postings and offset < expected_total:
                    raise RuntimeError(
                        f"SmartRecruiters pagination stalled at {offset}/{expected_total}")
        self.last_raw_count = raw_count
        self.last_raw_ids = set(seen_ids)
        return matches

    def hunt_workable(self, account, keywords=None):
        """Hunt Workable's documented public careers-page endpoint."""
        endpoint = f"https://www.workable.com/api/accounts/{account}"
        res = self._get(
            endpoint, params={"details": "false"}, timeout=20,
            headers={**API_HEADERS, "Accept": "application/json"})
        res.raise_for_status()
        payload = res.json()
        if (not isinstance(payload, dict) or not isinstance(payload.get("name"), str)
                or not isinstance(payload.get("jobs"), list)):
            raise ValueError("Workable response missing name/jobs[]")
        self.last_raw_count = len(payload["jobs"])
        matches = []
        seen_jobs = {}
        for job in payload["jobs"]:
            if not isinstance(job, dict):
                raise ValueError("Workable jobs[] contained a non-object entry")
            job_id = str(job.get("shortcode") or "")
            title = job.get("title")
            job_url = job.get("url") or job.get("shortlink")
            locations = job.get("locations")
            location_parts = []
            if isinstance(locations, list):
                for location in locations:
                    if not isinstance(location, dict):
                        raise ValueError("Workable job locations[] contained a non-object")
                    rendered = ", ".join(
                        str(location.get(field)).strip()
                        for field in ("city", "region", "country")
                        if location.get(field))
                    if rendered and rendered not in location_parts:
                        location_parts.append(rendered)
            if not location_parts:
                rendered = ", ".join(
                    str(job.get(field)).strip()
                    for field in ("city", "state", "country")
                    if job.get(field))
                if rendered:
                    location_parts.append(rendered)
            if job.get("telecommuting"):
                location_parts.append("Remote")
            location_text = " / ".join(location_parts)
            if (not job_id or not isinstance(title, str) or not title.strip()
                    or not isinstance(job_url, str) or not job_url or not location_text):
                raise ValueError("Workable job missing shortcode/title/url/location")
            # A shortcode reused for a different job is pagination/schema
            # corruption; an identical repeat is just a duplicated row.
            signature = (title, job_url)
            if job_id in seen_jobs:
                if seen_jobs[job_id] != signature:
                    raise RuntimeError(
                        "Workable reused a shortcode for conflicting jobs; "
                        "refusing an ambiguous read")
                continue
            seen_jobs[job_id] = signature
            if self.matches_criteria(title, location_text, keywords):
                matches.append({
                    "id": job_id, "title": title,
                    "location": location_text, "url": job_url,
                })
        self.last_raw_ids = set(seen_jobs)
        return matches

    def hunt_oracle_hcm(self, site_number, keyword="intern", location_id=None,
                        location=None, host="jpmc.fa.oraclecloud.com", max_pages=4,
                        queries=None, keywords=None):
        """Hunter for Oracle HCM Cloud portals. `host` selects the tenant
        (default JP Morgan); `keyword` and `location` narrow server-side.

        Oracle puts pagination inside the finder expression (`,offset=N`), not
        in the outer REST query.  Treat an incomplete four-page read as a
        failure so a large/truncated result set can never masquerade as a
        healthy zero-match source. `max_pages` is configurable for tenants
        whose fuzzy server search returns a large country-scoped set."""
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 4:
            raise ValueError("Oracle HCM max_pages must be an integer from 1 to 4")
        search_terms = queries if queries is not None else [keyword]
        if (not isinstance(search_terms, list) or not search_terms
                or any(not isinstance(value, str) or not value for value in search_terms)):
            raise ValueError("Oracle HCM queries must be a non-empty list of strings")
        url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        matches = []
        seen_job_ids = {}
        limit = 25
        requests_used = 0
        raw_count = 0
        for search_term in search_terms:
            if requests_used >= max_pages:
                # An earlier query used up the shared budget, so this one never
                # ran. Reporting it as "truncated (0/None)" would both blame a
                # query that made no request and throw away the matches the
                # earlier queries already found.
                raise RuntimeError(
                    f"Oracle HCM exhausted its {max_pages}-request budget before "
                    f"query '{search_term}' ran; narrow the queries or raise "
                    f"max_pages ({len(matches)} match(es) found before this point)")
            fetched = 0
            total = None
            page = 0
            while requests_used < max_pages:
                finder = f"findReqs;siteNumber={site_number},keyword={search_term}"
                if location_id:
                    finder += f",locationId={location_id}"
                elif location:
                    finder += f",location={location}"
                if page:
                    finder += f",offset={page * limit}"
                params = (
                    "onlyData=true&expand=requisitionList.secondaryLocations,"
                    "requisitionList.requisitionFlexFields&finder=" + finder + f"&limit={limit}"
                )
                res = self._get(f"{url}?{params}", timeout=15, headers=API_HEADERS)
                requests_used += 1
                res.raise_for_status()
                payload = res.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise ValueError("Oracle HCM response missing items[]")
                items = payload["items"]
                if not items or not isinstance(items[0], dict):
                    raise ValueError("Oracle HCM response missing search result")
                result = items[0]
                if "TotalJobsCount" not in result or not isinstance(result.get("requisitionList"), list):
                    raise ValueError("Oracle HCM response missing TotalJobsCount/requisitionList")
                total = int(result["TotalJobsCount"])
                reqs = result["requisitionList"]
                fetched += len(reqs)
                raw_count += len(reqs)
                for job in reqs:
                    if not isinstance(job, dict):
                        raise ValueError("Oracle HCM requisitionList contained a non-object entry")
                    job_id = str(job.get("Id") or "")
                    title = job.get("Title")
                    location_text = job.get("PrimaryLocation")
                    if (not job_id or not isinstance(title, str) or not title.strip()
                            or not isinstance(location_text, str)):
                        raise ValueError("Oracle HCM job missing Id/Title/PrimaryLocation")
                    signature = (title, location_text)
                    if job_id in seen_job_ids:
                        if seen_job_ids[job_id] != signature:
                            raise RuntimeError("Oracle HCM reused a stable id for conflicting jobs")
                        continue
                    seen_job_ids[job_id] = signature
                    if self.matches_criteria(title, location_text, keywords):
                        job_url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site_number}/job/{job_id}"
                        matches.append({"id": job_id, "title": title, "location": location_text, "url": job_url})
                page += 1
                if total == 0 or fetched >= total or not reqs:
                    break
            if total is None or fetched < total:
                raise RuntimeError(
                    f"Oracle HCM results truncated for query '{search_term}' ({fetched}/{total})")
        if requests_used > max_pages:
            raise RuntimeError("Oracle HCM exceeded its request budget")
        self.last_raw_count = raw_count
        self.last_raw_ids = set(seen_job_ids)
        return matches

    def hunt_workday(self, tenant, site, wd_host="wd5", search="intern",
                     include_multi_location=False, max_pages=4, keywords=None):
        """Hunter for Workday CXS boards (NVIDIA, Citi, BlackRock, Adobe, …).
        POSTs to the tenant's CXS jobs endpoint with server-side `searchText`
        so one page is usually enough; paginates only up to the request cap.

        Location filtering is client-side on `locationsText` (via
        matches_criteria). We deliberately DON'T send Workday's location facet:
        the India GUID is tenant-specific (NVIDIA 400s on the shared one), so a
        hardcoded facet is brittle — searchText + matches_criteria is portable.
        `include_multi_location` gates the "N Locations" inclusion rule below.

        `max_pages` is configurable because Workday hard-caps `limit` at 20
        (a larger limit 400s), so a tenant whose fuzzy `searchText` matches a
        big set — BlackRock's "intern" also hits "internal"/"international" —
        needs more than the default 4 pages to read its result set in full."""
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 12:
            raise ValueError("Workday max_pages must be an integer from 1 to 12")
        base = f"https://{tenant}.{wd_host}.myworkdayjobs.com"
        url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        limit = 20
        matches = []
        fetched = 0
        total = None
        seen_paths = set()
        raw_ids = set()
        # Cap the pagination loop hard so one source can't run away (plan §3).
        for page in range(max_pages):
            body = {"appliedFacets": {}, "limit": limit, "offset": page * limit, "searchText": search}
            res = self._post(url, json=body, timeout=15,
                             headers={**API_HEADERS, "Content-Type": "application/json", "Accept": "application/json"})
            res.raise_for_status()
            data = res.json()
            if (not isinstance(data, dict) or "total" not in data
                    or not isinstance(data.get("jobPostings"), list)):
                raise ValueError("Workday response missing total/jobPostings[]")
            # Trust `total` from the first page only. Some tenants (NVIDIA,
            # Citi) report total=0 on every offset>0 request, and overwriting
            # here made the truncation guard below compare against 0 — so a
            # partial read of a 901-posting board looked like a complete one.
            # A shrinking total must never shorten the read.
            page_total = int(data["total"])
            if total is None:
                total = page_total
            postings = data["jobPostings"]
            fetched += len(postings)
            for job in postings:
                if not isinstance(job, dict):
                    raise ValueError("Workday jobPostings[] contained a non-object entry")
                title = job.get("title")
                location = job.get("locationsText")
                path = job.get("externalPath")
                # A row with *every* field null is a tombstone — a pulled
                # requisition still occupying a slot, carrying only its id in
                # bulletFields (NVIDIA served one on 2026-08-03). It describes
                # no job, so skipping it loses nothing. A row with only *some*
                # fields missing is schema drift we don't understand, and still
                # fails the source loudly.
                if title is None and path is None and location is None:
                    continue
                if (not isinstance(title, str) or not title.strip()
                        or not isinstance(path, str) or not path):
                    raise ValueError("Workday posting missing title/externalPath")
                # A posting with no locationsText is a real Workday state, not
                # schema drift, so it must not fail the whole source. Treat it
                # like "N Locations": unknown location can't match a city, so
                # honour include_multi_location rather than silently dropping.
                if location is None:
                    location = ""
                elif not isinstance(location, str):
                    raise ValueError("Workday posting has a non-string locationsText")
                if path in seen_paths:
                    raise RuntimeError("Workday pagination repeated a posting; refusing an incomplete read")
                seen_paths.add(path)
                job_id = path.rsplit("_", 1)[-1] if "_" in path else path
                if not job_id:
                    raise ValueError("Workday posting has no stable identifier")
                raw_ids.add(job_id)
                # Multi-location postings collapse to "N Locations" — that text
                # can't match a city, so matches_criteria would drop an
                # India-eligible role. With include_multi_location set, include
                # such a posting on a title match alone (one extra alert beats a
                # silent miss — see plan §4.1). Otherwise honour the location filter.
                is_multi = (not location.strip()
                            or bool(re.match(r'^\d+\s+Locations$', location.strip())))
                if is_multi and include_multi_location:
                    if not self.title_matches(title, keywords):
                        continue
                elif not self.matches_criteria(title, location, keywords):
                    continue
                # Stable dedup key: trailing requisition id (e.g. JR1988855),
                # falling back to the full path so IDs never come from position.
                job_url = f"{base}/en-US/{site}{path}"
                matches.append({"id": job_id, "title": title, "location": location, "url": job_url})
            if (page + 1) * limit >= total or not postings:
                break
        if total is not None and fetched < total:
            raise RuntimeError(f"Workday results truncated ({fetched}/{total})")
        self.last_raw_count = fetched
        self.last_raw_ids = raw_ids
        return matches

    def hunt_amazon(self, query="intern", location="India", categories=None,
                    country_code="IND", keywords=None):
        """Hunter for amazon.jobs. One server-side filtered request is enough
        (Amazon's board is ~50k jobs, so query/loc params are mandatory).

        Location is filtered with `normalized_country_code[]`. The obvious
        `loc_query` is silently ignored by amazon.jobs — verified 2026-08-02,
        `loc_query=Germany` returns Italy/Poland/Mexico/USA — so relying on it
        meant fetching a globally-ranked page and hoping India survived the
        client-side check, which also put the truncation guard at the mercy of
        Amazon's *global* intern count. matches_criteria still re-checks
        client-side as defence in depth. `categories` narrows server-side via
        amazon.jobs' `category[]` param (live-verified: software-development /
        machine-learning-science / data-science) — without it Amazon India
        floods the digest with finance/ops interns."""
        url = (f"https://www.amazon.jobs/en/search.json?base_query={query}"
               f"&result_limit=100&offset=0")
        if country_code:
            url += f"&normalized_country_code[]={country_code}"
        for c in categories or []:
            url += f"&category[]={c}"
        # A browser-like UA keeps the JSON endpoint from 403-ing (plan §4.2).
        res = self._get(url, timeout=15, headers={
            **API_HEADERS,
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        res.raise_for_status()
        payload = res.json()
        if (not isinstance(payload, dict) or "hits" not in payload
                or not isinstance(payload.get("jobs"), list)):
            raise ValueError("Amazon response missing hits/jobs[]")
        total = int(payload["hits"])
        jobs = payload["jobs"]
        self.last_raw_count = len(jobs)
        raw_ids = set()
        if len(jobs) < total:
            raise RuntimeError(f"Amazon results truncated ({len(jobs)}/{total})")
        matches = []
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError("Amazon jobs[] contained a non-object entry")
            job_id = str(job.get("id_icims") or job.get("id") or "")
            raw_ids.add(job_id)
            title = job.get("title")
            location_text = job.get("normalized_location") or job.get("location")
            job_path = job.get("job_path")
            if (not job_id or not isinstance(title, str) or not title.strip()
                    or not isinstance(location_text, str)
                    or not isinstance(job_path, str) or not job_path):
                raise ValueError("Amazon job missing id/title/location/job_path")
            if self.matches_criteria(title, location_text, keywords):
                job_url = f"https://www.amazon.jobs{job_path}"
                matches.append({"id": job_id, "title": title, "location": location_text, "url": job_url})
        self.last_raw_ids = raw_ids
        return matches

    def hunt_atlassian(self, location="India", categories=None, keywords=None):
        """Read Atlassian's public careers listings feed directly.

        The all-jobs page's React widget can fail before it renders any cards,
        while this one-request JSON feed remains the page's source of truth.
        Category filters cover Interns/Graduates whose titles do not always
        contain a global keyword; exclude-keywords still apply.
        """
        url = "https://www.atlassian.com/endpoint/careers/listings"
        res = self._get(url, timeout=20, headers={**API_HEADERS, "Accept": "application/json"})
        res.raise_for_status()
        jobs = res.json()
        if not isinstance(jobs, list):
            raise ValueError("Atlassian response is not a listings array")
        self.last_raw_count = len(jobs)
        wanted_categories = {value.casefold() for value in (categories or [])}
        matches = []
        seen_ids = {}
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError("Atlassian listings contained a non-object entry")
            portal_id = str(job.get("portalId") or "")
            posting_id = str(job.get("id") or "")
            title = job.get("title")
            locations = job.get("locations")
            category = job.get("category")
            job_url = job.get("applyUrl")
            if (not portal_id or not posting_id or not isinstance(title, str) or not title.strip()
                    or not isinstance(locations, list) or any(not isinstance(x, str) for x in locations)
                    or not isinstance(category, str) or not isinstance(job_url, str) or not job_url):
                raise ValueError("Atlassian listing missing portalId/id/title/locations/category/applyUrl")
            job_id = f"{portal_id}:{posting_id}"
            signature = (title, tuple(locations), category, job_url)
            if job_id in seen_ids:
                if seen_ids[job_id] != signature:
                    raise ValueError("Atlassian listings reused a stable id for conflicting jobs")
                continue
            seen_ids[job_id] = signature
            location_text = ", ".join(value for value in locations if value)
            if not location_text.strip():
                raise ValueError("Atlassian listing has an empty location")
            # Word-boundary like location_matches elsewhere: a substring test
            # matched "india" inside "Indiana, United States".
            location_match = not location or bool(re.search(
                r'\b' + re.escape(location) + r'\b', location_text, re.IGNORECASE))
            # `categories` is a shortcut, not a gate: a listing in a wanted
            # category counts even if its title carries no keyword, but a
            # keyword title still counts on its own. Requiring both meant that
            # when Atlassian dropped its Interns/Graduates categories — the
            # feed carried neither on 2026-08-02 — the source could never match
            # anything and still reported a healthy zero.
            # Exclude-keywords gate BOTH paths: a wanted category must not
            # smuggle in an explicitly excluded title ("PhD Graduate").
            title_lower = self._title_for_matching(title)
            excluded_title = any(
                re.search(r'\b' + re.escape(k) + r'\b', title_lower)
                for k in self.exclude_keywords)
            in_category = bool(wanted_categories) and category.casefold() in wanted_categories
            title_match = self.title_matches(title, keywords)
            if not excluded_title and location_match and (in_category or title_match):
                matches.append({"id": job_id, "title": title, "location": location_text,
                                "url": job_url})
        self.last_raw_ids = set(seen_ids)
        return matches

    def hunt_eightfold(self, base_url, domain, query="intern", location="India",
                       seniority=None, keywords=None):
        """Hunter for public Eightfold/PCSX careers search endpoints.

        Used by Microsoft and Qualcomm.  The endpoint exposes an explicit
        count, which lets us distinguish a real zero from a malformed response
        and fail loudly if the four-request safety cap would truncate results.
        """
        matches = []
        start = 0
        total = None
        seen_ids = set()
        for _ in range(4):
            params = {"domain": domain, "query": query, "location": location, "start": start}
            if seniority:
                params["filter_seniority"] = seniority
            res = self._get(
                f"{base_url.rstrip('/')}/api/pcsx/search",
                params=params,
                timeout=15,
                headers={**API_HEADERS, "Accept": "application/json"},
            )
            res.raise_for_status()
            payload = res.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("positions"), list) or "count" not in data:
                raise ValueError("Eightfold response missing data.count/data.positions[]")
            positions = data["positions"]
            # Trust the count from the first page only: some tenants report
            # count=0 after the first offset, and overwriting here would make
            # a partial read look complete.
            if total is None:
                total = int(data["count"])
            for job in positions:
                if not isinstance(job, dict):
                    raise ValueError("Eightfold positions[] contained a non-object entry")
                job_id = str(job.get("id") or job.get("displayJobId") or "")
                position_url = job.get("positionUrl")
                title = job.get("name")
                if (not job_id or not isinstance(position_url, str) or not position_url
                        or not isinstance(title, str) or not title.strip()):
                    raise ValueError("Eightfold position missing id/positionUrl/name")
                if job_id in seen_ids:
                    raise RuntimeError("Eightfold pagination repeated a position; refusing an incomplete read")
                seen_ids.add(job_id)
                locs = job.get("locations") or job.get("standardizedLocations") or []
                if not isinstance(locs, list):
                    locs = [locs]
                location_parts = []
                for loc in locs:
                    if isinstance(loc, dict):
                        value = loc.get("name") or loc.get("displayName") or loc.get("location")
                    else:
                        value = str(loc)
                    if value:
                        location_parts.append(value)
                location_text = ", ".join(location_parts)
                if not location_text:
                    raise ValueError("Eightfold position missing recognized location data")
                if self.matches_criteria(title, location_text, keywords):
                    job_url = urljoin(base_url.rstrip("/") + "/", position_url)
                    matches.append({"id": job_id, "title": title, "location": location_text, "url": job_url})
            start += len(positions)
            if total == 0 or start >= total or not positions:
                break
        if total is not None and start < total:
            raise RuntimeError(f"Eightfold results truncated ({start}/{total})")
        self.last_raw_count = start
        self.last_raw_ids = set(seen_ids)
        return matches

    def hunt_microsoft(self, query="intern", location="India", keywords=None):
        """Backward-compatible Microsoft alias for the Eightfold adapter."""
        return self.hunt_eightfold(
            "https://apply.careers.microsoft.com",
            "microsoft.com",
            query=query,
            location=location,
            keywords=keywords,
        )

    def hunt_google(self, query="intern", location="India", keywords=None):
        """Parse Google's server-rendered official careers result cards."""
        base_url = "https://www.google.com/about/careers/applications/jobs/results/"
        next_url = base_url
        params = {"q": query, "location": location}
        matches = []
        fetched_pages = 0
        raw_count = 0
        raw_ids = set()
        while next_url and fetched_pages < 4:
            res = self._get(next_url, params=params if fetched_pages == 0 else None,
                               timeout=15, headers=API_HEADERS)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            cards = soup.select("li.lLd3Je")
            page_text = soup.get_text(" ", strip=True).lower()
            explicit_zero = bool(re.search(r"\b0\s+jobs?\s+matched\b", page_text))
            if not cards:
                if explicit_zero:
                    self.last_raw_count = raw_count
                    return matches
                raise ValueError("Google Careers page has neither job cards nor an explicit zero-result marker")
            parsed = 0
            for card in cards:
                title_el = card.select_one("h3.QJPWVe")
                link_el = card.select_one("a.WpHeLc[href]")
                if not title_el or not link_el:
                    raise ValueError("Google Careers card missing title/link structure")
                title = title_el.get_text(" ", strip=True)
                location_el = card.select_one(".r0wTof")
                if not title or not location_el:
                    raise ValueError("Google Careers card missing title/location")
                location_text = location_el.get_text(" ", strip=True)
                if not location_text:
                    raise ValueError("Google Careers card has an empty location")
                job_url = urljoin(base_url, link_el.get("href", ""))
                id_match = re.search(r"/results/(\d+)", job_url)
                if not id_match:
                    raise ValueError("Google Careers card missing stable result id")
                parsed += 1
                raw_count += 1
                raw_ids.add(id_match.group(1))
                if self.matches_criteria(title, location_text, keywords):
                    matches.append({"id": id_match.group(1), "title": title,
                                    "location": location_text, "url": job_url})
            if parsed == 0:
                raise ValueError("Google Careers cards found but none had the expected structure")
            fetched_pages += 1
            next_link = soup.select_one("a[aria-label='Go to next page'][href]")
            next_url = urljoin(base_url, next_link["href"]) if next_link else None
            params = None
        if next_url:
            raise RuntimeError("Google Careers results truncated after 4 pages")
        self.last_raw_count = raw_count
        self.last_raw_ids = raw_ids
        return matches

    def hunt_intuit(self, query="interns new college grads", location="India", keywords=None):
        """Parse Intuit's official TalentBrew search with an India geo filter."""
        base_url = "https://jobs.intuit.com/search-jobs"
        params = {
            "k": query, "l": location, "lat": "22.0", "lon": "79.0",
            "lt": "2", "lp": "1269750", "orgIds": "27595",
        }
        next_url = base_url
        matches = []
        page = 0
        expected_total = None
        parsed_total = 0
        seen_job_ids = set()
        while next_url and page < 4:
            res = self._get(next_url, params=params if page == 0 else None,
                               timeout=15, headers=API_HEADERS)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            results = soup.select_one("section#search-results")
            if not results or results.get("data-total-results") is None:
                raise ValueError("Intuit search page missing recognized results metadata")
            # TalentBrew can reset data-total-results on later offsets, even
            # while still serving cards. The first page's total stays
            # authoritative for both the zero check and truncation.
            if expected_total is None:
                expected_total = int(results["data-total-results"])
                if expected_total == 0:
                    self.last_raw_count = 0
                    return []
            cards = results.select("ul.search-list li a.sr-item[href]")
            if not cards:
                raise ValueError("Intuit reported results but exposed no job cards")
            for card in cards:
                title_el = card.select_one("h2")
                title = (card.get("data-title") or
                         (title_el.get_text(" ", strip=True) if title_el else ""))
                location_el = card.select_one("span.job-location")
                location_text = location_el.get_text(" ", strip=True) if location_el else ""
                job_url = urljoin(base_url, card.get("href", ""))
                job_id = job_url.rstrip("/").rsplit("/", 1)[-1]
                if not title or not job_id.isdigit():
                    raise ValueError("Intuit job card missing title/stable id")
                if job_id in seen_job_ids:
                    raise RuntimeError("Intuit pagination repeated a job; refusing an incomplete read")
                seen_job_ids.add(job_id)
                parsed_total += 1
                # The request is already country-filtered. TalentBrew sometimes
                # collapses an eligible card to "Multiple Locations".
                criteria_match = (self.title_matches(title, keywords)
                                  if location_text.lower() == "multiple locations"
                                  else self.matches_criteria(title, location_text, keywords))
                if criteria_match:
                    matches.append({"id": job_id, "title": title,
                                    "location": location_text, "url": job_url})
            page += 1
            next_link = results.select_one("nav.pagination a.next[href]:not(.disabled)")
            next_url = urljoin(base_url, next_link["href"]) if next_link else None
            params = None
        if expected_total is not None and parsed_total < expected_total:
            raise RuntimeError(
                f"Intuit results truncated ({parsed_total}/{expected_total})")
        self.last_raw_count = parsed_total
        self.last_raw_ids = set(seen_job_ids)
        return matches

    def hunt_goldman(self, search="summer analyst", location="India", keywords=None):
        """Query Goldman Sachs' official Higher campus GraphQL search."""
        endpoint = "https://api-higher.gs.com/gateway/api/v1/graphql"
        query = """query GetCampusRoles($searchQueryInput: RoleSearchQueryInput!) {
          roleSearch(searchQueryInput: $searchQueryInput) {
            totalCount
            items { roleId corporateTitle jobTitle jobFunction
              locations { primary state country city }
              status division externalSource { sourceId }
            }
          }
        }"""
        matches = []
        fetched = 0
        total = None
        seen_role_ids = set()
        for page in range(4):
            variables = {"searchQueryInput": {
                "page": {"pageSize": 20, "pageNumber": page},
                "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"},
                "filters": [{"filterCategoryType": "LOCATION",
                             "filters": [{"filter": location, "subFilters": []}]}],
                "experiences": ["CAMPUS"],
                "searchTerm": search,
            }}
            res = self._post(
                endpoint,
                json={"operationName": "GetCampusRoles", "query": query, "variables": variables},
                timeout=15,
                headers={**API_HEADERS, "Content-Type": "application/json",
                         "Accept": "application/json", "x-higher-request-id": str(uuid.uuid4())},
            )
            res.raise_for_status()
            payload = res.json()
            if isinstance(payload, dict) and payload.get("errors"):
                raise ValueError(f"Goldman Higher GraphQL error: {payload['errors'][0].get('message', 'unknown')}")
            result = payload.get("data", {}).get("roleSearch") if isinstance(payload, dict) else None
            if not isinstance(result, dict) or "totalCount" not in result or not isinstance(result.get("items"), list):
                raise ValueError("Goldman Higher response missing roleSearch totalCount/items[]")
            # First page's totalCount is authoritative; later pages have been
            # seen reporting 0 mid-pagination (same defect class as Workday).
            if total is None:
                total = int(result["totalCount"])
            items = result["items"]
            fetched += len(items)
            for job in items:
                if not isinstance(job, dict):
                    raise ValueError("Goldman Higher items[] contained a non-object entry")
                job_id = str(job.get("roleId") or "")
                title = job.get("jobTitle")
                locations = job.get("locations") or []
                if (not job_id or not isinstance(title, str) or not title.strip()
                        or not isinstance(locations, list)):
                    raise ValueError("Goldman Higher role missing roleId/jobTitle/locations[]")
                if job_id in seen_role_ids:
                    raise RuntimeError("Goldman Higher pagination repeated a role; refusing an incomplete read")
                seen_role_ids.add(job_id)
                location_text = "; ".join(
                    ", ".join(x for x in [loc.get("city"), loc.get("state"), loc.get("country")] if x)
                    for loc in locations if isinstance(loc, dict)
                )
                if not location_text:
                    raise ValueError("Goldman Higher role missing recognized location data")
                if self.matches_criteria(title, location_text, keywords):
                    matches.append({"id": job_id, "title": title, "location": location_text,
                                    "url": f"https://higher.gs.com/roles/{job_id}"})
            if total == 0 or fetched >= total or not items:
                break
        if total is not None and fetched < total:
            raise RuntimeError(f"Goldman Higher results truncated ({fetched}/{total})")
        self.last_raw_count = fetched
        self.last_raw_ids = set(seen_role_ids)
        return matches

    def hunt_deshaw(self, keywords=None):
        """Parse individual public internship cards from D. E. Shaw.

        This source is intentionally worldwide; D. E. Shaw India internships
        are campus-placement based and are covered separately by a program-page
        monitor.
        """
        base_url = "https://www.deshaw.com/careers/internships"
        res = self._get(base_url, timeout=20, headers=API_HEADERS)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        anchors = soup.select("a.parent-arrow-long[href*='/careers/']")
        if not anchors:
            text = soup.get_text(" ", strip=True).lower()
            if "no internships" in text or "no internship opportunities" in text:
                self.last_raw_count = 0
                return []
            raise ValueError("D. E. Shaw internships page missing recognized job cards")
        matches = []
        parsed = 0
        raw_ids = set()
        for anchor in anchors:
            raw = anchor.get_text(" ", strip=True)
            title = re.sub(r"^icon\s*", "", raw, flags=re.IGNORECASE).split(":", 1)[0].strip()
            if not title:
                raise ValueError("D. E. Shaw internship card missing title")
            job_url = urljoin(base_url, anchor.get("href", ""))
            id_match = re.search(r"-(\d+)(?:[/?#]|$)", job_url)
            if not id_match:
                raise ValueError("D. E. Shaw internship card missing stable id")
            parsed += 1
            raw_ids.add(id_match.group(1))
            if not self.title_matches(title, keywords):
                continue
            location_match = re.search(r"\(([^)]+)\)", title)
            location_text = location_match.group(1) if location_match else "Worldwide"
            matches.append({"id": id_match.group(1), "title": title,
                            "location": location_text, "url": job_url})
        # Cards existed but only false positives such as "Internal ..." is a
        # valid zero after exact word-boundary title filtering.
        self.last_raw_count = parsed
        self.last_raw_ids = raw_ids
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
    lowered = stripped.casefold()
    blocked_markers = (
        "access denied", "verify you are human", "captcha", "just a moment",
        "service unavailable", "internal server error", "error 404", "page not found",
    )
    marker = next((value for value in blocked_markers if value in lowered), None)
    if marker:
        return f"page contains error/block marker: {marker}"
    return None

def ats_request_group(comp):
    """Return a politeness group shared by sources on the same ATS host."""
    ats_type = comp.get("ats", "unknown")
    if ats_type in {"greenhouse", "ashby", "lever", "smartrecruiters", "workable"}:
        return ats_type
    if ats_type == "workday":
        return f"workday:{comp.get('tenant', '')}.{comp.get('wd_host', 'wd5')}.myworkdayjobs.com"
    if ats_type == "eightfold":
        return f"eightfold:{urlparse(str(comp.get('base_url', ''))).netloc.casefold()}"
    if ats_type == "oracle_hcm":
        return f"oracle_hcm:{str(comp.get('host', '')).casefold()}"
    if ats_type == "amazon":
        return "amazon:amazon.jobs"
    return ats_type


def _hunt_ats_entry(comp, config, semaphores):
    """Fetch one ATS source in a worker without touching shared state."""
    hunter = ATSHunter(config)
    started = time.monotonic()
    try:
        with semaphores[ats_request_group(comp)]:
            ats_type = comp["ats"]
            kw_override = comp.get("keywords")
            if kw_override is not None:
                kw_override = [k.lower() for k in kw_override]
            if ats_type == "ashby":
                matches = hunter.hunt_ashby(comp["slug"], keywords=kw_override)
            elif ats_type == "lever":
                matches = hunter.hunt_lever(comp["slug"], keywords=kw_override)
            elif ats_type == "greenhouse":
                matches = hunter.hunt_greenhouse(comp["slug"], keywords=kw_override)
            elif ats_type == "smartrecruiters":
                matches = hunter.hunt_smartrecruiters(
                    company_id=comp["company_id"], country=comp.get("country"),
                    query=comp.get("query"), queries=comp.get("queries"),
                    keywords=kw_override)
            elif ats_type == "workable":
                matches = hunter.hunt_workable(comp["account"], keywords=kw_override)
            elif ats_type == "oracle_hcm":
                matches = hunter.hunt_oracle_hcm(
                    site_number=comp.get("site_number", "CX_1001"),
                    keyword=comp.get("keyword", "intern"),
                    location_id=comp.get("location_id"), location=comp.get("location"),
                    host=comp.get("host", "jpmc.fa.oraclecloud.com"),
                    max_pages=comp.get("max_pages", 4), queries=comp.get("queries"),
                    keywords=kw_override)
            elif ats_type == "workday":
                matches = hunter.hunt_workday(
                    tenant=comp["tenant"], site=comp["site"],
                    wd_host=comp.get("wd_host", "wd5"),
                    search=comp.get("search", "intern"),
                    include_multi_location=comp.get("include_multi_location", False),
                    max_pages=comp.get("max_pages", 4), keywords=kw_override)
            elif ats_type == "amazon":
                matches = hunter.hunt_amazon(
                    query=comp.get("query", "intern"),
                    location=comp.get("location", "India"),
                    categories=comp.get("categories"),
                    country_code=comp.get("country_code", "IND"),
                    keywords=kw_override)
            elif ats_type == "atlassian":
                matches = hunter.hunt_atlassian(
                    location=comp.get("location", "India"),
                    categories=comp.get("categories", ["Interns", "Graduates"]),
                    keywords=kw_override)
            elif ats_type == "microsoft":
                matches = hunter.hunt_microsoft(
                    query=comp.get("query", "intern"),
                    location=comp.get("location", "India"), keywords=kw_override)
            elif ats_type == "eightfold":
                matches = hunter.hunt_eightfold(
                    base_url=comp["base_url"], domain=comp["domain"],
                    query=comp.get("query", "intern"),
                    location=comp.get("location", "India"),
                    seniority=comp.get("seniority"), keywords=kw_override)
            elif ats_type == "google":
                matches = hunter.hunt_google(
                    query=comp.get("query", "intern"),
                    location=comp.get("location", "India"), keywords=kw_override)
            elif ats_type == "intuit":
                matches = hunter.hunt_intuit(
                    query=comp.get("query", "interns new college grads"),
                    location=comp.get("location", "India"), keywords=kw_override)
            elif ats_type == "goldman_higher":
                matches = hunter.hunt_goldman(
                    search=comp.get("search", "summer analyst"),
                    location=comp.get("location", "India"), keywords=kw_override)
            elif ats_type == "deshaw":
                matches = hunter.hunt_deshaw(keywords=kw_override)
            else:
                raise ValueError(f"unknown ats type '{ats_type}'")
        return {
            "comp": comp, "matches": matches, "error": None,
            "raw_count": hunter.last_raw_count,
            "raw_ids": set(hunter.last_raw_ids),
            "request_count": hunter.request_count,
            "duration": time.monotonic() - started,
        }
    except Exception as exc:
        return {
            "comp": comp, "matches": None, "error": exc,
            "raw_count": hunter.last_raw_count,
            "raw_ids": set(hunter.last_raw_ids),
            "request_count": hunter.request_count,
            "duration": time.monotonic() - started,
        }


class CustomWebHunter:
    def __init__(self, config):
        self.keywords = [k.lower() for k in config.get("keywords", [])]
        self.locations = [l.lower() for l in config.get("locations", [])]
        self.exclude_locations = [l.lower() for l in config.get("exclude_locations") or []]
        self.criteria = ATSHunter(config)
        self._playwright = None
        self._browser = None

    def _ensure_browser(self):
        """Lazily launch one Chromium process and reuse it for this run."""
        if self._browser is not None:
            return self._browser
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise
        return self._browser

    def close(self):
        """Close the shared browser lifecycle; safe to call repeatedly."""
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        try:
            if browser is not None:
                browser.close()
        finally:
            if playwright is not None:
                playwright.stop()

    def parse_structured_jobs(self, content, page_config):
        """Parse a rendered job board using selectors from custom-page config."""
        url = page_config["url"]
        job_selector = page_config["job_selector"]
        keyword_filter = page_config.get("keyword_filter", False)
        location_filter = page_config.get("location_filter", True)
        location_filter = page_config.get("location_filter", True)
        soup = BeautifulSoup(content, "html.parser")
        cards = soup.select(job_selector)
        body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
        zero_markers = page_config.get("zero_result_text") or []
        if isinstance(zero_markers, str):
            zero_markers = [zero_markers]
        recognized_zero = any(marker.lower() in body_text.lower() for marker in zero_markers)
        if not cards:
            if recognized_zero:
                return {"jobs": []}
            return {"failed": f"job selector matched nothing: {job_selector}"}

        jobs = []
        seen_urls = set()
        for card in cards:
            link_selector = page_config.get("link_selector")
            if link_selector:
                link_el = card.select_one(link_selector)
            elif card.name == "a" and card.get("href"):
                link_el = card
            else:
                link_el = card.select_one("a[href]")
            title_selector = page_config.get("title_selector")
            title_el = card.select_one(title_selector) if title_selector else None
            location_selector = page_config.get("job_location_selector")
            location_el = card.select_one(location_selector) if location_selector else None
            if title_selector and not title_el:
                raise ValueError("structured custom-page card missing configured title selector")
            if location_selector and not location_el:
                raise ValueError("structured custom-page card missing configured location selector")
            title = ""
            if title_el:
                title = title_el.get_text(" ", strip=True)
            elif link_el:
                title = link_el.get("data-title") or link_el.get_text(" ", strip=True)
            location_text = location_el.get_text(" ", strip=True) if location_el else ""
            if not link_el or not title:
                raise ValueError("structured custom-page card missing title/link")
            href = link_el.get("href")
            if not isinstance(href, str) or not href.strip():
                raise ValueError("structured custom-page card has no link href")
            job_url = urljoin(url, href)
            if not job_url or job_url in seen_urls:
                continue
            seen_urls.add(job_url)
            id_pattern = page_config.get("id_regex")
            if id_pattern:
                id_match = re.search(id_pattern, job_url)
                if not id_match:
                    raise ValueError("structured custom-page job URL missing configured id")
                job_id = id_match.group(1)
            else:
                job_id = job_url
            if not job_id:
                raise ValueError("structured custom-page job has an empty stable id")
            # Filters are independently configurable: each one applies its own
            # check only when enabled, so (keyword_filter=False) must not
            # silently drag the title check back in via matches_criteria.
            if keyword_filter and location_filter:
                criteria_match = self.criteria.matches_criteria(title, location_text)
            elif keyword_filter:
                criteria_match = self.criteria.title_matches(title)
            elif location_filter:
                criteria_match = self.criteria.location_matches(location_text)
            else:
                criteria_match = True
            if criteria_match:
                jobs.append({"id": job_id, "title": title,
                             "location": location_text or "India (source-filtered)",
                             "url": job_url})
        return {"jobs": jobs}

    def hunt(self, page_config, attempts=2):
        """Render a custom page, retrying once on a transient failure.

        Browser navigation fails for reasons that have nothing to do with the
        source — a dropped connection, a slow CDN, a Chromium crash that takes
        the shared browser process with it and would otherwise fail every page
        queued behind it. One retry, with the browser rebuilt in between, keeps
        a blip from entering the failure streak while still reporting a page
        that is genuinely broken: a real failure fails both attempts."""
        last = None
        for attempt in range(max(1, attempts)):
            last = self._hunt_once(page_config)
            if not last.get("failed"):
                return last
            if attempt + 1 < attempts:
                print(f"  ↻ retrying {page_config['name']}: {last['failed']}")
                # The shared Chromium may be the thing that died; drop it so the
                # next attempt starts a fresh one.
                self.close()
        return last

    def _hunt_once(self, page_config):
        url = page_config["url"]
        wait_for = page_config.get("wait_for_selector")
        css_selector = page_config.get("css_selector")
        job_selector = page_config.get("job_selector")
        keyword_filter = page_config.get("keyword_filter", False)
        location_filter = page_config.get("location_filter", True) # Default to true for safety
        navigation_timeout = page_config.get("navigation_timeout", 30)
        selector_timeout = page_config.get("selector_timeout", 10)
        render_delay = page_config.get("render_delay", 3)
        wait_timed_out = False
        page = None

        try:
            browser = self._ensure_browser()
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            response = page.goto(
                url, wait_until="domcontentloaded",
                timeout=int(navigation_timeout * 1000))
            if response is None:
                raise RuntimeError("navigation returned no HTTP response")
            if not response.ok:
                raise RuntimeError(f"page returned HTTP {response.status}")

            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=int(selector_timeout * 1000))
                except Exception:
                    wait_timed_out = True
                    print(f"Timeout waiting for selector {wait_for} on {url}")

            page.wait_for_timeout(int(render_delay * 1000))
            content = page.content()

            # A selector timeout means the page never finished rendering, so
            # whatever is in `content` is a partial read. Checked before the
            # structured branch: parsing a half-rendered board yields a short
            # job list that looks like a complete one — the silent partial read
            # this crawler exists to avoid.
            if wait_timed_out:
                return {"failed": f"timeout waiting for configured selector: {wait_for}"}

            # Structured Playwright source: return individual jobs instead of
            # a generic page hash. This keeps JS-only boards (Atlassian) in the
            # four-hour browser path while preserving normal job-level dedup.
            if job_selector:
                return self.parse_structured_jobs(content, page_config)

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
                # Word-boundary, matching the ATS path, so "india" can't hit
                # "Indiana" in a footer. Deliberately does NOT apply
                # exclude_locations: this is whole-page text, and a careers page
                # that mentions "USA" anywhere is not thereby a non-India page.
                matches_locations = any(
                    re.search(r'\b' + re.escape(l) + r'\b', text_lower)
                    for l in self.locations)
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
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass


def company_filters_from_argv(argv):
    """Return case-folded `--company NAME` filters (repeatable)."""
    filters = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--company":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise ValueError("--company requires a source name")
            filters.append(argv[i + 1].casefold())
            i += 2
            continue
        if arg.startswith("--company="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise ValueError("--company requires a source name")
            filters.append(value.casefold())
        i += 1
    return filters


def option_value_from_argv(argv, option):
    """Read one ``--option value`` or ``--option=value`` occurrence."""
    values = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == option:
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                raise ValueError(f"{option} requires a value")
            values.append(argv[index + 1])
            index += 2
            continue
        if arg.startswith(option + "="):
            value = arg.split("=", 1)[1]
            if not value:
                raise ValueError(f"{option} requires a value")
            values.append(value)
        index += 1
    if len(values) > 1:
        raise ValueError(f"{option} may only be supplied once")
    return values[0] if values else None


def shard_settings_from_argv(argv):
    """Return validated ``(index, count, state_path)`` shard settings."""
    try:
        count = int(option_value_from_argv(argv, "--shard-count") or "1")
        index = int(option_value_from_argv(argv, "--shard-index") or "0")
    except ValueError as exc:
        if "requires" in str(exc) or "supplied" in str(exc):
            raise
        raise ValueError("--shard-count and --shard-index must be integers") from exc
    if count < 1:
        raise ValueError("--shard-count must be at least 1")
    if index < 0 or index >= count:
        raise ValueError("--shard-index must be between 0 and shard-count - 1")
    explicit_state = option_value_from_argv(argv, "--state-file")
    if explicit_state:
        if os.path.isabs(explicit_state) or os.path.dirname(explicit_state):
            raise ValueError("--state-file must be a filename in the working directory")
        state_path = explicit_state
    elif count > 1:
        state_path = f"state.shard-{index}-of-{count}.json"
    else:
        state_path = STATE_FILE
    return index, count, state_path


def source_shard(entry, shard_count, alias_map=None):
    """Assign related sources to the same deterministic shard. A relation
    target written as an alias resolves to the canonical parent first, so a
    parent and its monitors always hash to the same shard."""
    raw = (entry.get("fallback_for")
           or entry.get("supplement_for")
           or entry.get("name", ""))
    key = str(raw).casefold()
    if alias_map:
        resolved = alias_map.get(key)
        if resolved:
            key = resolved
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def source_in_shard(entry, shard_index, shard_count, alias_map=None):
    return (shard_count == 1
            or source_shard(entry, shard_count, alias_map) == shard_index)


def source_selected(entry, filters):
    if not filters:
        return True
    names = [entry.get("name", ""), entry.get("fallback_for", ""), entry.get("supplement_for", "")]
    names.extend(entry.get("aliases") or [])
    folded = {name.casefold() for name in names if name}
    return any(value in folded for value in filters)


def resolve_company_filters(config, filters):
    """Validate filters and expand aliases to their canonical source names.
    A source tied to a selected parent via fallback_for/supplement_for rides
    along, so `--company Atlassian` also hunts its fallback monitor."""
    if not filters:
        return []
    entries = list(config.get("ats_companies") or []) + list(config.get("custom_pages") or [])
    expanded = set(filters)
    unknown = []
    canonical_names = set()
    alias_map = {}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            continue
        canonical_names.add(name.casefold())
        for alias in entry.get("aliases") or []:
            if isinstance(alias, str) and alias:
                alias_map.setdefault(alias.casefold(), name.casefold())

    def canonical(value):
        return value if value in canonical_names else alias_map.get(value)

    # Related sources join the selection; a related source can itself be a
    # parent (chained monitors), so iterate until the set stops growing.
    changed = True
    while changed:
        changed = False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            relation = entry.get("fallback_for") or entry.get("supplement_for")
            if not isinstance(relation, str) or not relation:
                continue
            target = canonical(relation.casefold())
            name = entry.get("name", "").casefold()
            if target and target in expanded and name and name not in expanded:
                expanded.add(name)
                changed = True

    for value in filters:
        hits = []
        for entry in entries:
            names = [entry.get("name", ""), *(entry.get("aliases") or [])]
            if value in {name.casefold() for name in names if name}:
                hits.append(entry)
        if not hits:
            unknown.append(value)
            continue
        expanded.update(entry["name"].casefold() for entry in hits)
    if unknown:
        raise ValueError("unknown --company source(s): " + ", ".join(sorted(unknown)))
    return sorted(expanded)


# Fields an adapter reads via comp[...] (no default) — a missing one only
# surfaces at hunt time as a per-source ❌. `--validate` catches it up front.
REQUIRED_ATS_FIELDS = {
    "ashby": ("slug",), "lever": ("slug",), "greenhouse": ("slug",),
    "smartrecruiters": ("company_id",),
    "workable": ("account",),
    "workday": ("tenant", "site"),
    "eightfold": ("base_url", "domain"),
    "oracle_hcm": ("site_number", "host"),
}
KNOWN_ATS_TYPES = set(REQUIRED_ATS_FIELDS) | {
    "amazon", "atlassian", "google", "intuit", "goldman_higher", "deshaw", "microsoft",
}


def validate_config(config):
    """Preflight lint for config.yaml. Returns a list of problem strings."""
    problems = []
    if not isinstance(config, dict):
        return ["config.yaml top level must be a mapping"]
    concurrency = config.get("concurrency", DEFAULT_CONCURRENCY)
    if (not isinstance(concurrency, int) or isinstance(concurrency, bool)
            or not 1 <= concurrency <= MAX_CONCURRENCY):
        problems.append(
            f"concurrency must be an integer from 1 to {MAX_CONCURRENCY}")
    for key in ("keywords", "exclude_keywords", "locations", "exclude_locations"):
        value = config.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            problems.append(f"{key} must be a list of strings")
        elif any(not item.strip() for item in value):
            # A whitespace-only keyword can never match a word boundary, so it
            # is always a config typo — and an empty exclude_location would
            # match every location and veto everything.
            problems.append(
                f"{key} must not contain empty or whitespace-only entries")
    telegram = config.get("telegram", {})
    if not isinstance(telegram, dict):
        problems.append("telegram must be a mapping")

    ats_entries = config.get("ats_companies") or []
    custom_entries = config.get("custom_pages") or []
    if not isinstance(ats_entries, list):
        problems.append("ats_companies must be a list")
        ats_entries = []
    if not isinstance(custom_entries, list):
        problems.append("custom_pages must be a list")
        custom_entries = []

    seen_names = set()
    canonical_names = set()
    aliases = {}
    relationships = []
    for entry in ats_entries:
        if not isinstance(entry, dict):
            problems.append(f"ats_companies entry must be a mapping: {entry!r}")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"ats_companies entry missing name: {entry}")
            continue
        if name.casefold() in seen_names:
            problems.append(f"duplicate source name: {name}")
        seen_names.add(name.casefold())
        canonical_names.add(name.casefold())
        ats = entry.get("ats")
        if ats not in KNOWN_ATS_TYPES:
            problems.append(f"{name}: unknown ats type '{ats}'")
            continue
        for field in REQUIRED_ATS_FIELDS.get(ats, ()):
            if not entry.get(field):
                problems.append(f"{name}: ats '{ats}' requires '{field}'")
        if ats == "eightfold" and entry.get("base_url") \
                and not re.match(r"^https?://", str(entry["base_url"])):
            problems.append(f"{name}: base_url must start with http:// or https://")
        if "keywords" in entry and (
                not isinstance(entry["keywords"], list)
                or any(not isinstance(item, str) for item in entry["keywords"])):
            problems.append(f"{name}: keywords must be a list of strings")
        elif "keywords" in entry and any(not item.strip() for item in entry["keywords"]):
            problems.append(
                f"{name}: keywords must not contain empty or whitespace-only entries")
        if "categories" in entry and (
                not isinstance(entry["categories"], list)
                or any(not isinstance(item, str) or not item for item in entry["categories"])):
            problems.append(f"{name}: categories must be a list of non-empty strings")
        if "queries" in entry and (
                not isinstance(entry["queries"], list) or not entry["queries"]
                or any(not isinstance(item, str) or not item for item in entry["queries"])):
            problems.append(f"{name}: queries must be a non-empty list of strings")
        if "max_pages" in entry:
            # Workday needs a higher ceiling than the other paginated adapters
            # because its `limit` is capped at 20 server-side (see hunt_workday).
            ceiling = 12 if ats == "workday" else 4
            value = entry["max_pages"]
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= ceiling:
                problems.append(
                    f"{name}: max_pages must be an integer from 1 to {ceiling}")
        entry_aliases = entry.get("aliases") or []
        if not isinstance(entry_aliases, list) or any(not isinstance(a, str) or not a for a in entry_aliases):
            problems.append(f"{name}: aliases must be a list of non-empty strings")
        else:
            for alias in entry_aliases:
                owner = aliases.setdefault(alias.casefold(), name)
                if owner != name:
                    problems.append(f"duplicate alias '{alias}' used by {owner} and {name}")

    # Two sources reading the same board under different names would each
    # alert on the same job: dedup keys are (source name, job id), so a
    # shared slug/tenant defeats job-level dedup entirely.
    board_identities = {}
    for entry in ats_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        ats = entry.get("ats")
        if ats in ("ashby", "lever", "greenhouse"):
            key = (ats, str(entry.get("slug") or ""))
        elif ats == "workday":
            key = (ats, str(entry.get("tenant") or ""),
                   str(entry.get("wd_host") or ""), str(entry.get("site") or ""))
        elif ats == "eightfold":
            key = (ats, str(entry.get("base_url") or "").rstrip("/"),
                   str(entry.get("domain") or ""))
        elif ats == "smartrecruiters":
            # Same company_id scoped to different countries is legitimate;
            # the country is part of what the adapter requests.
            key = (ats, str(entry.get("company_id") or ""), str(entry.get("country") or ""))
        elif ats == "workable":
            key = (ats, str(entry.get("account") or ""))
        elif ats == "oracle_hcm":
            key = (ats, str(entry.get("host") or ""), str(entry.get("site_number") or ""))
        else:
            continue
        board_identities.setdefault(key, []).append(entry["name"])
    for key, names in sorted(board_identities.items()):
        if len(names) > 1:
            problems.append(
                f"duplicate board target: {' and '.join(names)} "
                f"all read {key[0]}:" + "/".join(part for part in key[1:]))

    for entry in custom_entries:
        if not isinstance(entry, dict):
            problems.append(f"custom_pages entry must be a mapping: {entry!r}")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"custom_pages entry missing name: {entry}")
            continue
        if name.casefold() in seen_names:
            problems.append(f"duplicate source name: {name}")
        seen_names.add(name.casefold())
        canonical_names.add(name.casefold())
        url = entry.get("url")
        if not isinstance(url, str) or not re.match(r"^https?://", url):
            problems.append(f"{name}: custom page missing 'url'")
        for field in ("keyword_filter", "location_filter"):
            if field in entry and not isinstance(entry[field], bool):
                problems.append(f"{name}: {field} must be true or false")
        for field, minimum, maximum in (
                ("navigation_timeout", 1, 60),
                ("selector_timeout", 1, 30),
                ("render_delay", 0, 15)):
            if field in entry:
                value = entry[field]
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not minimum <= value <= maximum):
                    problems.append(
                        f"{name}: {field} must be a number from {minimum} to {maximum}")
        for field in ("job_selector", "title_selector", "link_selector",
                      "job_location_selector", "wait_for_selector", "css_selector"):
            if field in entry and (not isinstance(entry[field], str) or not entry[field]):
                problems.append(f"{name}: {field} must be a non-empty string")
        if any(field in entry for field in ("title_selector", "link_selector",
                                             "job_location_selector", "id_regex")) \
                and not entry.get("job_selector"):
            problems.append(f"{name}: structured job fields require job_selector")
        if entry.get("id_regex"):
            try:
                compiled = re.compile(entry["id_regex"])
                if compiled.groups < 1:
                    problems.append(f"{name}: id_regex must contain a capture group")
            except (re.error, TypeError) as e:
                problems.append(f"{name}: invalid id_regex: {e}")
        zero_markers = entry.get("zero_result_text")
        if zero_markers is not None:
            if isinstance(zero_markers, str):
                zero_markers = [zero_markers]
            if not isinstance(zero_markers, list) or any(not isinstance(x, str) or not x.strip() for x in zero_markers):
                problems.append(f"{name}: zero_result_text must be a string or list of non-empty strings")
        for relation in ("fallback_for", "supplement_for"):
            if entry.get(relation):
                relationships.append((name, relation, entry[relation]))
        if entry.get("fallback_for") and entry.get("supplement_for"):
            problems.append(f"{name}: cannot set both fallback_for and supplement_for")
        entry_aliases = entry.get("aliases") or []
        if not isinstance(entry_aliases, list) or any(not isinstance(a, str) or not a for a in entry_aliases):
            problems.append(f"{name}: aliases must be a list of non-empty strings")
        else:
            for alias in entry_aliases:
                owner = aliases.setdefault(alias.casefold(), name)
                if owner != name:
                    problems.append(f"duplicate alias '{alias}' used by {owner} and {name}")

    all_identifiers = canonical_names | set(aliases)
    for alias, owner in aliases.items():
        if alias in canonical_names and alias != owner.casefold():
            problems.append(f"alias '{alias}' for {owner} conflicts with a source name")
    for name, relation, target in relationships:
        if not isinstance(target, str) or target.casefold() not in all_identifiers:
            problems.append(f"{name}: {relation} references unknown source '{target}'")
    return problems


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
    suspects = []
    for name, entry in (state_manager.state.get("_health") or {}).items():
        if entry.get("suspect"):
            suspects.append((name, entry["suspect"]))
        elif entry.get("successful_zero_streak", 0) >= 24:
            suspects.append((name, f"{entry['successful_zero_streak']} consecutive successful zero-match runs"))
    for name, reason in suspects:
        heartbeat.append(f"❓ {html.escape(name)}: {html.escape(reason)}")
    if not failing and not suspects:
        heartbeat.append("All sources healthy ✅")
    if not notifier.send("\n".join(heartbeat)):
        raise RuntimeError("heartbeat could not be delivered to Telegram")

USAGE = """\
usage: hybrid_hunter.py [options]

Modes (default: live run — hunts every source and sends Telegram alerts):
  --test                Hunt and print matches; no state writes, no alerts
  --seed                Baseline current postings into state without alerting
  --validate            Lint config.yaml and exit
  --heartbeat           Send the status digest to Telegram and exit
  -h, --help            Show this message and exit

Scope:
  --ats-only            Skip Playwright-rendered custom pages
  --pages-only          Skip structured ATS adapters
  --company NAME        Restrict to one source (repeatable; accepts aliases)
  --workers N            Maximum concurrent ATS sources (default 8)

Sharding:
  --shard-count N       Split sources into N deterministic shards (default 1)
  --shard-index I       Run shard I (0-based; default 0)
  --state-file NAME     Override the state filename (working directory only)
"""

# Every accepted flag. An unrecognised flag must abort rather than be ignored:
# the workflow builds this command line dynamically, and silently dropping a
# typo'd `--test` would turn an intended dry run into a live alerting run.
KNOWN_FLAGS = {
    "--test", "--seed", "--validate", "--heartbeat", "--help", "-h",
    "--ats-only", "--pages-only", "--company",
    "--shard-count", "--shard-index", "--state-file", "--workers",
}
VALUE_FLAGS = {
    "--company", "--shard-count", "--shard-index", "--state-file", "--workers",
}


def unknown_flags_from_argv(argv):
    """Return argv entries that are not accepted flags or their values.
    Boolean flags reject `=` forms outright: `--test=false` reads like a dry
    run but is a typo'd invocation whose intent we cannot guess."""
    unknown = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if not arg.startswith("-"):
            unknown.append(arg)
            continue
        name, equals, _ = arg.partition("=")
        if name not in KNOWN_FLAGS:
            unknown.append(arg)
            continue
        if name in VALUE_FLAGS:
            if not equals:
                skip_next = True
        elif equals:
            unknown.append(arg)
    return unknown


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE, end="")
        raise SystemExit(0)
    unknown = unknown_flags_from_argv(sys.argv[1:])
    if unknown:
        raise SystemExit(
            "unknown argument(s): " + ", ".join(unknown) + "\n\n" + USAGE)

    # Run modes are exclusive by definition: e.g. --seed after --test would
    # silently turn a dry run into a state-writing baseline, so combining
    # them must abort rather than let flag order decide.
    mode_flags = [flag for flag in ("--test", "--seed", "--heartbeat")
                  if flag in sys.argv]
    if len(mode_flags) > 1:
        raise SystemExit(
            f"{' and '.join(mode_flags)} are mutually exclusive; pick one.\n\n" + USAGE)
    scope_flags = [flag for flag in ("--ats-only", "--pages-only") if flag in sys.argv]
    if len(scope_flags) > 1:
        raise SystemExit(
            f"{' and '.join(scope_flags)} are mutually exclusive: "
            "together they select no sources.\n\n" + USAGE)

    test_mode = "--test" in sys.argv
    seed_mode = "--seed" in sys.argv
    hunt_ats = "--pages-only" not in sys.argv
    hunt_pages = "--ats-only" not in sys.argv
    try:
        company_filters = company_filters_from_argv(sys.argv[1:])
        shard_index, shard_count, state_path = shard_settings_from_argv(sys.argv[1:])
    except ValueError as e:
        raise SystemExit(str(e))

    if not os.path.exists("config.yaml"):
        raise SystemExit("config.yaml not found! Please create one based on the template.")

    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    problems = validate_config(config)
    if "--validate" in sys.argv:
        for problem in problems:
            print(f"❌ {problem}")
        if not problems:
            print("✅ config.yaml is valid")
        raise SystemExit(1 if problems else 0)
    if problems:
        details = "\n".join(f"❌ {problem}" for problem in problems)
        raise SystemExit(f"config.yaml validation failed:\n{details}")

    try:
        company_filters = resolve_company_filters(config, company_filters)
    except ValueError as e:
        raise SystemExit(str(e))

    try:
        if (shard_count > 1 and not test_mode and not seed_mode
                and not os.path.exists(state_path)):
            raise RuntimeError(
                f"{state_path} does not exist; initialize this shard with "
                f"--seed --shard-index {shard_index} --shard-count {shard_count} "
                "before its first live run")
        state_manager = StateManager(state_path)
    except RuntimeError as e:
        raise SystemExit(f"❌ {e}") from e
    notifier = Notifier(config)
    if not test_mode and not seed_mode and not notifier.enabled:
        raise SystemExit(
            "❌ TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required for live and heartbeat runs; "
            "refusing to mark alerts as delivered")
    custom_hunter = CustomWebHunter(config)

    if "--heartbeat" in sys.argv:
        try:
            send_heartbeat(config, state_manager, notifier)
        except RuntimeError as e:
            raise SystemExit(f"❌ {e}") from e
        return

    if test_mode:
        print("="*60)
        print("🧪 TEST MODE — No state changes, no notifications")
        print(f"Keywords: {config.get('keywords', [])}")
        print(f"Locations: {config.get('locations', [])}")
        if company_filters:
            print(f"Selected sources: {', '.join(company_filters)}")
        if shard_count > 1:
            print(f"Shard: {shard_index + 1}/{shard_count} (state: {state_path})")
        print("="*60)
    elif seed_mode:
        print("="*60)
        print("🌱 SEED MODE — Baselining state, no notifications")
        print("="*60)
    else:
        state_manager.flush_pending(notifier)
        # Checkpoint the flush immediately: a successful redelivery must be
        # recorded before the crawl can fail, or a crash would resend the
        # same alert next run (delivery-before-dedup, extended to retries).
        state_manager.save_if_dirty()

    new_jobs = []       # matches to notify, marked seen only after delivery
    page_changes = []   # custom-page changes, hash saved only after delivery
    run_report = []     # (source, status) per source, for the Actions summary
    recovered = []      # sources that came back after an alerted failure streak
    seen_this_run = set()
    raw_results = {}
    seeded_jobs = 0
    seeded_pages = 0
    source_failures = 0

    # alias -> canonical owner name, so shard assignment can group a parent
    # with monitors whose fallback_for/supplement_for is written as an alias.
    alias_map = {}
    for entry in list(config.get("ats_companies") or []) + list(config.get("custom_pages") or []):
        owner = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(owner, str) and owner:
            for entry_alias in entry.get("aliases") or []:
                if isinstance(entry_alias, str) and entry_alias:
                    alias_map.setdefault(entry_alias.casefold(), owner.casefold())

    # 1. Process ATS Companies
    ats_companies = (config.get("ats_companies") or []) if hunt_ats else []
    ats_companies = [entry for entry in ats_companies if source_selected(entry, company_filters)]
    ats_companies = [
        entry for entry in ats_companies
        if source_in_shard(entry, shard_index, shard_count, alias_map)
    ]
    backoff_ats = []
    if not test_mode and not company_filters:
        eligible_ats = []
        for entry in ats_companies:
            if state_manager.source_in_backoff(entry["name"]):
                backoff_ats.append(entry)
            else:
                eligible_ats.append(entry)
        ats_companies = eligible_ats
    custom_pages = (config.get("custom_pages") or []) if hunt_pages else []
    custom_pages = [entry for entry in custom_pages if source_selected(entry, company_filters)]
    custom_pages = [
        entry for entry in custom_pages
        if source_in_shard(entry, shard_index, shard_count, alias_map)
    ]
    backoff_pages = []
    if not test_mode and not company_filters:
        eligible_pages = []
        for entry in custom_pages:
            if state_manager.source_in_backoff(entry["name"]):
                backoff_pages.append(entry)
            else:
                eligible_pages.append(entry)
        custom_pages = eligible_pages
    if company_filters and not ats_companies and not custom_pages:
        raise SystemExit("no selected sources are enabled by the requested --ats-only/--pages-only mode")
    if hunt_ats:
        print("\n📡 Hunting ATS boards...")
    else:
        print("\n📡 Skipping ATS boards (--pages-only)")
    concurrency = config.get("concurrency", DEFAULT_CONCURRENCY)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) \
            or not 1 <= concurrency <= MAX_CONCURRENCY:
        raise SystemExit(
            f"config concurrency must be an integer from 1 to {MAX_CONCURRENCY}")
    worker_arg = option_value_from_argv(sys.argv[1:], "--workers")
    if worker_arg is not None:
        try:
            concurrency = int(worker_arg)
        except ValueError as exc:
            raise SystemExit("--workers must be an integer") from exc
        if not 1 <= concurrency <= MAX_CONCURRENCY:
            raise SystemExit(f"--workers must be from 1 to {MAX_CONCURRENCY}")

    semaphores = {}
    for comp in ats_companies:
        semaphores.setdefault(ats_request_group(comp), threading.Semaphore(2))
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_hunt_ats_entry, comp, config, semaphores): index
            for index, comp in enumerate(ats_companies)
        }
        for future in as_completed(futures):
            result = future.result()
            result["index"] = futures[future]
            results.append(result)

    # Workers complete out of order, but state/digest updates and the visible
    # result table remain in config order for reproducible Actions summaries.
    for completed_ats, result in enumerate(
            sorted(results, key=lambda item: item["index"]), 1):
        comp = result["comp"]
        name, ats_type = comp["name"], comp["ats"]
        error = result["error"]
        matches = result["matches"]
        if error is not None:
            print(f"  ❌ {name} ({ats_type}): {error}")
            run_report.append((name, f"❌ {error}"))
            if not test_mode:
                state_manager.record_failure(name, error)
                state_manager.record_source_run(
                    name, ats_type, False, None, result["duration"],
                    result["request_count"], reason=error,
                    raw_count=result.get("raw_count"))
            source_failures += 1
            if not test_mode and completed_ats % 25 == 0:
                state_manager.save_if_dirty()
            continue

        raw_results[name] = result.get("raw_ids", set())
        if not test_mode:
            state_manager.record_source_run(
                name, ats_type, True, len(matches), result["duration"],
                result["request_count"], raw_count=result.get("raw_count"))
            if state_manager.record_success(name):
                recovered.append(name)
        suspect = (
            state_manager.state.get("_health", {}).get(name, {}).get("suspect")
            if not test_mode else None)
        run_report.append((
            name,
            f"? {'✅' if matches else '—'} {len(matches)} match(es)"
            if suspect else f"✅ {len(matches)} match(es)"))

        if test_mode:
            print(f"  {'✅' if matches else '—'} {name} ({ats_type}): {len(matches)} matches")
            for m in matches[:3]:
                print(f"      → {m['title']} | {m['location']}")
            if len(matches) > 3:
                print(f"      ... and {len(matches) - 3} more")
            if not test_mode and completed_ats % 25 == 0:
                state_manager.save_if_dirty()
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
        if not test_mode and completed_ats % 25 == 0:
            state_manager.save_if_dirty()

    # Persist source health/failure progress before Playwright work begins.
    if not test_mode:
        state_manager.save_if_dirty()

    # 2. Process Custom Web Pages
    if hunt_pages:
        print("\n🌐 Hunting Custom Web Pages (Playwright JS-rendered)...")
    else:
        print("\n🌐 Skipping custom pages (--ats-only)")
    for page_config in custom_pages:
        name = page_config["name"]
        url = page_config["url"]
        source_started = time.monotonic()

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
                state_manager.record_source_run(
                    name, "custom_page", False, None,
                    time.monotonic() - source_started, 1, reason=failed_reason)
            source_failures += 1
            if not test_mode:
                state_manager.save_if_dirty()
            continue

        health_match_count = (
            len(result["jobs"]) if "jobs" in result
            else int(bool(result.get("is_match")))
        )
        if not test_mode:
            state_manager.record_source_run(
                name, "custom_page", True, health_match_count,
                time.monotonic() - source_started, 1)
            if state_manager.record_success(name):
                recovered.append(name)

        if "jobs" in result:
            matches = result["jobs"]
            raw_results[name] = {match["id"] for match in matches}
            run_report.append((name, f"✅ {len(matches)} match(es)"))
            if test_mode:
                print(f"  {'✅' if matches else '—'} {name} (browser jobs): {len(matches)} matches")
                for match in matches[:3]:
                    print(f"      → {match['title']} | {match['location']}")
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
            if not test_mode:
                state_manager.save_if_dirty()
            continue

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
            state_manager.save_if_dirty()
            continue
        if not state_manager.hash_changed(name, result["hash"]):
            run_report.append((name, "✅ no change"))
            state_manager.save_if_dirty()
            continue
        # Confirm the change before alerting. A hash is only as stable as the
        # render that produced it: a slow-loading page can serve enough text to
        # pass page_text_failure while still being incomplete, and that partial
        # text hashes differently every time. Re-render once and require the
        # new hash to reproduce; a real content change reproduces, a rendering
        # artefact does not. Costs an extra load only when a change is seen.
        confirmation = custom_hunter.hunt(page_config)
        if confirmation.get("failed") or confirmation.get("hash") != result["hash"]:
            detail = confirmation.get("failed") or "hash did not reproduce"
            print(f"  ~ {name}: change not confirmed ({detail}); leaving baseline alone")
            run_report.append((name, f"~ unconfirmed change ({detail})"))
            state_manager.save_if_dirty()
            continue
        run_report.append((name, "✅ CHANGED"))
        if seed_mode:
            state_manager.set_hash(name, result["hash"])
            seeded_pages += 1
        else:
            page_changes.append({"name": name, "url": url, "hash": result["hash"]})
        state_manager.save_if_dirty()

    custom_hunter.close()

    for entry in backoff_ats + backoff_pages:
        name = entry["name"]
        print(f"  ⏸ {name}: skipped (backoff)")
        run_report.append((name, "⏸ skipped (backoff)"))

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
        # Deliberately limited to the derived failure/health rows: per-source
        # job ids stay, so temporarily removing and re-adding a source can't
        # replay its whole board as "new".
        pruned = state_manager.prune_removed_sources(
            [entry.get("name", "") for entry in
             list(config.get("ats_companies") or [])
             + list(config.get("custom_pages") or [])])
        if pruned:
            print(f"🧹 Pruned state for removed source(s): {', '.join(pruned)}")
        if not seed_mode:
            expired = state_manager.prune_expired_jobs(raw_results)
            if expired:
                print(f"🧹 Pruned {len(expired)} expired job id(s)")
        state_manager.save_if_dirty()
    write_step_summary(run_report, new_jobs, state_manager)

    if source_failures:
        print(f"\n❌ Done with {source_failures} source failure(s).")
    else:
        print("\n✅ Done!")
    if seed_mode:
        print(
            f"🌱 Seeded {seeded_jobs} job(s) and {seeded_pages} page baseline(s) "
            f"into {state_manager.path}")
    if source_failures:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
