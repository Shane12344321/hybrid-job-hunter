"""Probe which ATS a company uses and emit a ready-to-paste config.yaml entry.

Usage:
    python3 probe.py "Company Name"      # try slug candidates on Greenhouse/Ashby/Lever
    python3 probe.py <careers URL>       # recognize Workday / Greenhouse / Lever /
                                         # Ashby / Eightfold / amazon.jobs from the URL
    python3 probe.py "Company Name" <careers URL>
    python3 probe.py --batch candidates.yaml --output probe-report.yaml

Read-only by default: never touches config.yaml, state.json, or the input
candidate ledger. Batch mode performs bounded-concurrency probing and writes
a review report only when --output is supplied. The explicit --merge-report
operation updates lifecycle/probe fields in a YAML ledger. add_source.py drives this
programmatically; .agents/skills/add-source covers the cases probing cannot
handle (JS-only boards and program-monitor pages).
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

TIMEOUT = 12
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

# Words dropped for the "trimmed" slug candidate ("Tower Research Capital LLC"
# → also try "towerresearchcapital").
SUFFIX_WORDS = {"inc", "llc", "ltd", "labs", "corp", "co", "technologies"}

GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io",
                    "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
LEVER_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}
ASHBY_HOSTS = {"jobs.ashbyhq.com"}
SUPPORTED_LEDGER_STATUSES = {"candidate", "probed", "verified", "seeded", "active"}
UNSUPPORTED_ATS_HOST_PATTERNS = (
    ("successfactors", ("successfactors.com", "successfactors.eu")),
    ("icims", ("icims.com",)),
    ("jobvite", ("jobvite.com",)),
    ("taleo", ("taleo.net",)),
    ("adp", ("adp.com",)),
    ("phenom", ("phenompeople.com",)),
    ("avature", ("avature.net",)),
    ("personio", ("personio.de", "personio.com")),
    ("bamboohr", ("bamboohr.com",)),
)


def slug_candidates(name):
    """Ordered, deduped slug guesses for a company name."""
    base = re.sub(r"[^a-z0-9 ]+", "", name.lower()).strip()
    words = base.split()
    cands = []
    if words:
        cands.append("".join(words))
        if len(words) > 1:
            cands.append("-".join(words))
            cands.append(words[0])
        trimmed = [w for w in words if w not in SUFFIX_WORDS]
        if trimmed and trimmed != words:
            cands.append("".join(trimmed))
    seen, out = set(), []
    for cand in cands:
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def slug_is_high_confidence(name, slug):
    """True when a slug represents the full company name, not one generic word."""
    base = re.sub(r"[^a-z0-9 ]+", "", name.lower()).strip()
    words = base.split()
    return slug in {"".join(words), "-".join(words)}


def _json_or_none(res):
    try:
        return res.json()
    except ValueError:
        return None


def check_greenhouse(slug):
    """Returns the live job count for a valid board slug, else None."""
    res = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                       timeout=TIMEOUT, headers=HEADERS)
    if res.status_code != 200:
        return None
    payload = _json_or_none(res)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return len(jobs) if isinstance(jobs, list) else None


def check_lever(slug):
    res = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                       timeout=TIMEOUT, headers=HEADERS)
    if res.status_code != 200:
        return None
    payload = _json_or_none(res)
    return len(payload) if isinstance(payload, list) else None


def check_ashby(slug):
    res = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                       timeout=TIMEOUT, headers=HEADERS)
    if res.status_code != 200:
        return None
    payload = _json_or_none(res)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return len(jobs) if isinstance(jobs, list) else None


def check_smartrecruiters(company_id, query=None):
    params = {"limit": 1, "offset": 0}
    if query:
        params["q"] = query
    res = requests.get(
        f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings",
        params=params,
        timeout=TIMEOUT, headers=HEADERS)
    if res.status_code != 200:
        return None


def check_workable(account):
    res = requests.get(
        f"https://www.workable.com/api/accounts/{account}",
        params={"details": "false"}, timeout=TIMEOUT, headers=HEADERS)
    if res.status_code != 200:
        return None
    payload = _json_or_none(res)
    if (not isinstance(payload, dict) or not isinstance(payload.get("name"), str)
            or not isinstance(payload.get("jobs"), list)):
        return None
    return len(payload["jobs"])
    payload = _json_or_none(res)
    if (not isinstance(payload, dict) or "totalFound" not in payload
            or not isinstance(payload.get("content"), list)):
        return None
    try:
        return int(payload["totalFound"])
    except (TypeError, ValueError):
        return None


def check_workday(tenant, wd_host, site):
    """Verify a Workday CXS board with the same POST the hunter uses."""
    url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    res = requests.post(url, json={"limit": 1, "offset": 0, "searchText": "intern"},
                        timeout=TIMEOUT,
                        headers={**HEADERS, "Content-Type": "application/json"})
    if res.status_code != 200:
        return None
    payload = _json_or_none(res)
    if (isinstance(payload, dict) and "total" in payload
            and isinstance(payload.get("jobPostings"), list)):
        return int(payload["total"])
    return None


def check_eightfold(base_url, domain):
    res = requests.get(f"{base_url.rstrip('/')}/api/pcsx/search",
                       params={"domain": domain, "query": "intern",
                               "location": "India", "start": 0},
                       timeout=TIMEOUT, headers=HEADERS)
    if res.status_code != 200:
        return None
    payload = _json_or_none(res)
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and isinstance(data.get("positions"), list) and "count" in data:
        return int(data["count"])
    return None


SLUG_CHECKERS = [("greenhouse", check_greenhouse),
                 ("ashby", check_ashby),
                 ("lever", check_lever)]


def probe_name(name):
    """Try every slug candidate against the three slug-based ATS APIs.
    Returns a list of {"ats", "slug", "jobs"} hits (possibly empty)."""
    hits = []
    for slug in slug_candidates(name):
        for ats, checker in SLUG_CHECKERS:
            try:
                count = checker(slug)
            except requests.RequestException:
                count = None
            if count is not None:
                hits.append({"ats": ats, "slug": slug, "jobs": count})
    return hits


def probe_name_detailed(name):
    """Probe a name without turning transport failures into a silent miss.

    A checker returning ``None`` means that one slug/ATS combination is not a
    board. A RequestException is retained in ``errors`` so batch callers can
    distinguish "not found" from an unreliable probe run.
    """
    hits = []
    errors = []
    for slug in slug_candidates(name):
        for ats, checker in SLUG_CHECKERS:
            try:
                count = checker(slug)
            except requests.RequestException as exc:
                errors.append(f"{ats}/{slug}: {exc}")
                continue
            if count is not None:
                hits.append({"ats": ats, "slug": slug, "jobs": count})
    return hits, errors


def _registrable_domain(host):
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def fingerprint_unsupported_url(url):
    """Identify a known-but-unsupported ATS from a careers URL."""
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").casefold()
    for ats, suffixes in UNSUPPORTED_ATS_HOST_PATTERNS:
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return ats
    return None


def _looks_like_supported_board_url(url):
    host = (urlparse(url).hostname or "").casefold()
    return (
        host in GREENHOUSE_HOSTS | LEVER_HOSTS | ASHBY_HOSTS
        or host in {
            "amazon.jobs", "www.amazon.jobs",
            "jobs.smartrecruiters.com", "careers.smartrecruiters.com",
            "apply.workable.com",
        }
        or host.endswith(".myworkdayjobs.com")
        or fingerprint_unsupported_url(url) is not None
    )


def discover_careers_urls(domain):
    """Try three conventional company careers locations and extract ATS links."""
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("company_domain must be a non-empty domain")
    parsed = urlparse("https://" + domain.strip().removeprefix("https://").removeprefix("http://"))
    host = parsed.hostname
    if not host or parsed.path not in ("", "/"):
        raise ValueError("company_domain must contain only a hostname")
    targets = [
        f"https://{host}/careers",
        f"https://{host}/jobs",
        f"https://careers.{host}",
    ]
    found = []
    fallbacks = []
    errors = []
    html_headers = {
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.8",
    }
    for target in targets:
        try:
            response = requests.get(
                target, timeout=TIMEOUT, headers=html_headers, allow_redirects=True)
        except requests.RequestException as exc:
            errors.append(f"{target}: {exc}")
            continue
        if response.status_code == 404:
            continue
        if response.status_code >= 400:
            errors.append(f"{target}: HTTP {response.status_code}")
            continue
        final_url = response.url or target
        if _looks_like_supported_board_url(final_url) and final_url not in found:
            found.append(final_url)
        elif final_url not in fallbacks:
            fallbacks.append(final_url)
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type and "<a" not in response.text[:10000].lower():
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select("a[href]"):
            href = urljoin(final_url, anchor.get("href"))
            if _looks_like_supported_board_url(href) and href not in found:
                found.append(href)
    return found + [url for url in fallbacks if url not in found], errors


def probe_url(url, name=None):
    """Recognize an ATS from a careers URL. Returns (entry, live_count) on
    success or (None, reason) on failure. `live_count` is None when the URL
    was recognized but not countable here (amazon.jobs)."""
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    segs = [s for s in parsed.path.split("/") if s]

    wd = re.match(r"^([\w-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if wd:
        tenant, wd_host = wd.group(1), wd.group(2)
        site_segs = [s for s in segs if not re.match(r"^[a-z]{2}-[A-Z]{2}$", s)]
        if not site_segs:
            return None, "Workday URL has no site path segment (need https://TENANT.wdN.myworkdayjobs.com/SITE)"
        site = site_segs[0]
        count = check_workday(tenant, wd_host, site)
        if count is None:
            return None, f"Workday CXS verify failed (tenant={tenant}, wd_host={wd_host}, site={site})"
        return ({"name": name or tenant, "ats": "workday",
                 "tenant": tenant, "wd_host": wd_host, "site": site}, count)

    for hosts, ats, checker in ((GREENHOUSE_HOSTS, "greenhouse", check_greenhouse),
                                (LEVER_HOSTS, "lever", check_lever),
                                (ASHBY_HOSTS, "ashby", check_ashby)):
        if host in hosts:
            if not segs:
                return None, f"{ats} URL has no board slug in the path"
            slug = segs[0]
            count = checker(slug)
            if count is None:
                return None, f"{ats} API rejected slug '{slug}'"
            return ({"name": name or slug, "ats": ats, "slug": slug}, count)

    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        if not segs:
            return None, "SmartRecruiters URL has no company identifier in the path"
        company_id = segs[0]
        count = check_smartrecruiters(company_id)
        if count is None:
            return None, f"SmartRecruiters API rejected company identifier '{company_id}'"
        return ({
            "name": name or company_id,
            "ats": "smartrecruiters",
            "company_id": company_id,
        }, count)

    if host == "apply.workable.com":
        if not segs:
            return None, "Workable URL has no account identifier in the path"
        account = segs[0]
        count = check_workable(account)
        if count is None:
            return None, f"Workable public API rejected account '{account}'"
        return ({
            "name": name or account,
            "ats": "workable",
            "account": account,
        }, count)

    if host in ("amazon.jobs", "www.amazon.jobs"):
        return ({"name": name or "Amazon", "ats": "amazon"}, None)

    # Anything else: try the site itself as a public Eightfold/PCSX board.
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    domain = _registrable_domain(host)
    try:
        count = check_eightfold(base_url, domain)
    except requests.RequestException:
        count = None
    if count is not None:
        return ({"name": name or domain, "ats": "eightfold",
                 "base_url": base_url, "domain": domain,
                 "query": "intern", "location": "India"}, count)

    return None, ("URL not recognized as a supported ATS. If the board is "
                  "JS-rendered, add it as a custom page instead (see "
                  ".agents/skills/add-source and diagnose.py).")


def yaml_entry(entry):
    """Render one entry as a config.yaml ats_companies list item."""
    return yaml.dump([entry], sort_keys=False, width=1000)


def _looks_like_url(arg):
    return "://" in arg or (("." in arg) and (" " not in arg))


def load_candidates(path):
    """Load a CSV or YAML candidate ledger and validate its portable schema."""
    extension = os.path.splitext(path)[1].lower()
    if extension == ".csv":
        with open(path, newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    elif extension in (".yaml", ".yml"):
        with open(path, encoding="utf-8") as stream:
            payload = yaml.safe_load(stream)
        if isinstance(payload, dict):
            rows = payload.get("candidates")
        else:
            rows = payload
        if rows is None:
            rows = []
    else:
        raise ValueError("candidate ledger must be .csv, .yaml, or .yml")

    if not isinstance(rows, list):
        raise ValueError("candidate ledger must contain a list of candidates")
    candidates = []
    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"candidate {index} must be a mapping")
        candidate = {key: value for key, value in row.items() if value not in (None, "")}
        name = candidate.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"candidate {index} is missing a name")
        name = name.strip()
        if name.casefold() in seen:
            raise ValueError(f"duplicate candidate name: {name}")
        seen.add(name.casefold())
        candidate["name"] = name
        url = candidate.get("careers_url") or candidate.get("url")
        if url is not None and (not isinstance(url, str) or not _looks_like_url(url)):
            raise ValueError(f"{name}: careers_url must be an HTTP(S) URL")
        if url:
            candidate["careers_url"] = url
            candidate.pop("url", None)
        status = candidate.get("status", "candidate")
        if status not in SUPPORTED_LEDGER_STATUSES:
            raise ValueError(
                f"{name}: status must be one of {', '.join(sorted(SUPPORTED_LEDGER_STATUSES))}")
        candidate["status"] = status
        candidates.append(candidate)
    return candidates


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def probe_candidate(candidate):
    """Probe one ledger row and return review data without mutating the row."""
    result = dict(candidate)
    for field in (
            "probe_status", "last_probed_at", "reason", "warnings",
            "live_postings", "suggested_entry", "alternatives", "unsupported_ats",
            "discovered_careers_url"):
        result.pop(field, None)
    result["last_probed_at"] = _utc_now()
    name = candidate["name"]
    url = candidate.get("careers_url")
    if not url and candidate.get("company_domain"):
        try:
            discovered, discovery_errors = discover_careers_urls(
                candidate["company_domain"])
        except (ValueError, requests.RequestException) as exc:
            result.update({
                "probe_status": "failed",
                "reason": f"careers URL discovery failed: {exc}",
            })
            return result
        if not discovered:
            result.update({
                "status": "probed" if not discovery_errors else candidate.get("status", "candidate"),
                "probe_status": "not_found" if not discovery_errors else "failed",
                "reason": (
                    "no supported ATS link found at conventional careers paths"
                    if not discovery_errors
                    else "careers URL discovery was unreliable: " + "; ".join(discovery_errors[:3])
                ),
            })
            return result
        verified = []
        unsupported = []
        for discovered_url in discovered[:3]:
            try:
                entry, info = probe_url(discovered_url, name=name)
            except requests.RequestException as exc:
                discovery_errors.append(f"{discovered_url}: {exc}")
                continue
            if entry:
                verified.append((discovered_url, entry, info))
            else:
                unsupported.append((discovered_url, info))
        if verified:
            deduped = {}
            for item in verified:
                key = yaml.safe_dump(item[1], sort_keys=True)
                deduped.setdefault(key, item)
            verified = list(deduped.values())
            discovered_url, entry, info = verified[0]
            result.update({
                "status": "probed",
                "probe_status": (
                    "needs_review" if len(verified) > 1 else "verified_endpoint"),
                "careers_url": discovered_url,
                "discovered_careers_url": discovered_url,
                "live_postings": info,
                "suggested_entry": entry,
            })
            if len(verified) > 1:
                result["alternatives"] = [
                    {"careers_url": item[0], "suggested_entry": item[1],
                     "live_postings": item[2]}
                    for item in verified
                ]
            return result
        result.update({
            "status": "probed" if not discovery_errors else candidate.get("status", "candidate"),
            "probe_status": "unsupported" if unsupported else "failed",
            "careers_url": discovered[0],
            "discovered_careers_url": discovered[0],
            "reason": (
                unsupported[0][1] if unsupported
                else "discovered careers links could not be verified: "
                     + "; ".join(discovery_errors[:3])
            ),
        })
        unsupported_ats = fingerprint_unsupported_url(discovered[0])
        if unsupported_ats:
            result["unsupported_ats"] = unsupported_ats
        return result
    if url:
        try:
            entry, info = probe_url(url, name=name)
        except requests.RequestException as exc:
            result.update({
                "probe_status": "failed",
                "reason": f"network failure: {exc}",
            })
            return result
        if not entry:
            unsupported_ats = fingerprint_unsupported_url(url)
            result.update({
                "status": "probed",
                "probe_status": "unsupported",
                "reason": info,
            })
            if unsupported_ats:
                result["unsupported_ats"] = unsupported_ats
            return result
        result.update({
            "status": "probed",
            "probe_status": "verified_endpoint",
            "live_postings": info,
            "suggested_entry": entry,
        })
        return result

    hits, errors = probe_name_detailed(name)
    unique_hits = {
        (hit["ats"], hit["slug"]): hit
        for hit in hits
    }
    hits = list(unique_hits.values())
    if not hits:
        if errors:
            result.update({
                "probe_status": "failed",
                "reason": "all or part of the probe was unreliable: " + "; ".join(errors[:3]),
            })
        else:
            result.update({
                "status": "probed",
                "probe_status": "not_found",
                "reason": "no Greenhouse, Ashby, or Lever board matched the name",
            })
        return result

    ranked = sorted(hits, key=lambda hit: (-hit["jobs"], hit["ats"], hit["slug"]))
    best = ranked[0]
    result.update({
        "status": "probed",
        "probe_status": (
            "needs_review"
            if len(ranked) > 1 or not slug_is_high_confidence(name, best["slug"])
            else "verified_endpoint"
        ),
        "live_postings": best["jobs"],
        "suggested_entry": {
            "name": name, "ats": best["ats"], "slug": best["slug"],
        },
    })
    if len(ranked) > 1:
        result["alternatives"] = ranked
    if errors:
        result["warnings"] = errors[:3]
    if not slug_is_high_confidence(name, best["slug"]):
        result.setdefault("warnings", []).append(
            f"lossy slug '{best['slug']}' does not represent the full company name")
    return result


def batch_probe(path, workers=4, statuses=None, with_domain=False):
    """Probe every candidate with bounded concurrency and return a report."""
    if not 1 <= workers <= 16:
        raise ValueError("--workers must be between 1 and 16")
    candidates = load_candidates(path)
    if statuses:
        statuses = set(statuses)
        candidates = [
            candidate for candidate in candidates
            if candidate.get("status", "candidate") in statuses
        ]
    if with_domain:
        candidates = [
            candidate for candidate in candidates if candidate.get("company_domain")
        ]
    results = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(probe_candidate, candidate): index
            for index, candidate in enumerate(candidates)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                # Unexpected checker/schema bugs must remain visible in the
                # report rather than aborting the remaining candidate probes.
                results[index] = {
                    **candidates[index],
                    "last_probed_at": _utc_now(),
                    "probe_status": "failed",
                    "reason": f"unexpected probe failure: {exc}",
                }

    status_counts = {}
    ats_counts = {}
    unsupported_counts = {}
    for result in results:
        probe_status = result["probe_status"]
        status_counts[probe_status] = status_counts.get(probe_status, 0) + 1
        entry = result.get("suggested_entry") or {}
        if entry.get("ats"):
            ats = entry["ats"]
            ats_counts[ats] = ats_counts.get(ats, 0) + 1
        unsupported_ats = result.get("unsupported_ats")
        if unsupported_ats:
            unsupported_counts[unsupported_ats] = unsupported_counts.get(unsupported_ats, 0) + 1
    return {
        "version": 1,
        "generated_at": _utc_now(),
        "source": os.path.basename(path),
        "summary": {
            "total": len(results),
            "probe_status": dict(sorted(status_counts.items())),
            "ats_fingerprints": dict(sorted(
                ats_counts.items(), key=lambda item: (-item[1], item[0]))),
            "blocked_ats_fingerprints": dict(sorted(
                unsupported_counts.items(), key=lambda item: (-item[1], item[0]))),
        },
        "candidates": results,
    }


def write_report(report, path):
    extension = os.path.splitext(path)[1].lower()
    if extension == ".json":
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    elif extension in (".yaml", ".yml"):
        with open(path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(report, stream, sort_keys=False, width=1000)
    else:
        raise ValueError("--output must end in .json, .yaml, or .yml")


def merge_probe_report(ledger_path, report_path):
    """Merge review evidence into a YAML ledger without touching config/state."""
    if os.path.splitext(ledger_path)[1].lower() not in (".yaml", ".yml"):
        raise ValueError("--merge-report requires a YAML ledger")
    candidates = load_candidates(ledger_path)
    with open(report_path, encoding="utf-8") as stream:
        report = yaml.safe_load(stream)
    results = report.get("candidates") if isinstance(report, dict) else None
    if not isinstance(results, list):
        raise ValueError("probe report must contain candidates[]")
    by_name = {
        row.get("name", "").casefold(): row
        for row in results if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    fields = (
        "probe_status", "last_probed_at", "reason", "warnings",
        "live_postings", "suggested_entry", "alternatives", "unsupported_ats",
        "careers_url", "discovered_careers_url",
    )
    merged = 0
    for candidate in candidates:
        result = by_name.get(candidate["name"].casefold())
        if not result:
            continue
        merged += 1
        if candidate.get("status") != "active" and result.get("probe_status") != "failed":
            candidate["status"] = "probed"
        for field in fields:
            if field in result:
                candidate[field] = result[field]
            else:
                candidate.pop(field, None)
    with open(ledger_path, "w", encoding="utf-8") as stream:
        yaml.safe_dump(
            {"version": 1, "candidates": candidates},
            stream, sort_keys=False, width=1000)
    return merged


def print_batch_summary(report):
    summary = report["summary"]
    print(f"Probed {summary['total']} candidate(s)")
    for status, count in summary["probe_status"].items():
        print(f"  {status}: {count}")
    if summary["ats_fingerprints"]:
        print("ATS fingerprints:")
        for ats, count in summary["ats_fingerprints"].items():
            print(f"  {ats}: {count}")
    if summary["blocked_ats_fingerprints"]:
        print("Unsupported ATS fingerprints (adapter roadmap):")
        for ats, count in summary["blocked_ats_fingerprints"].items():
            print(f"  {ats}: {count}")
    for candidate in report["candidates"]:
        marker = "✅" if candidate["probe_status"] == "verified_endpoint" else "⚠️"
        entry = candidate.get("suggested_entry") or {}
        detail = entry.get("ats") or candidate.get("reason", "")
        print(f"{marker} {candidate['name']}: {candidate['probe_status']} {detail}".rstrip())


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="*",
                        help="Company name and optionally its careers URL")
    parser.add_argument("--batch", metavar="PATH",
                        help="Probe a CSV/YAML candidate ledger without changing it")
    parser.add_argument("--workers", type=int, default=4,
                        help="Maximum concurrent batch probes (1-16; default 4)")
    parser.add_argument("--output",
                        help="Write the batch review report as .yaml or .json")
    parser.add_argument("--status", action="append", choices=sorted(SUPPORTED_LEDGER_STATUSES),
                        help="With --batch, probe only candidates in this status; repeatable")
    parser.add_argument("--with-domain", action="store_true",
                        help="With --batch, probe only rows that define company_domain")
    parser.add_argument("--merge-report", metavar="REPORT",
                        help="Merge an existing report into the YAML --batch ledger and exit")
    parsed = parser.parse_args()

    if parsed.batch:
        if parsed.targets:
            parser.error("positional targets cannot be combined with --batch")
        try:
            if parsed.merge_report:
                if parsed.output or parsed.status or parsed.with_domain:
                    parser.error(
                        "--merge-report cannot be combined with --output, --status, or --with-domain")
                merged = merge_probe_report(parsed.batch, parsed.merge_report)
                print(f"Merged probe evidence for {merged} candidate(s) into {parsed.batch}")
                return
            report = batch_probe(
                parsed.batch, parsed.workers, statuses=parsed.status,
                with_domain=parsed.with_domain)
            print_batch_summary(report)
            if parsed.output:
                write_report(report, parsed.output)
                print(f"Review report written to {parsed.output}")
        except (OSError, ValueError) as exc:
            raise SystemExit(f"❌ {exc}") from exc
        failed = report["summary"]["probe_status"].get("failed", 0)
        raise SystemExit(1 if failed else 0)

    if parsed.output:
        parser.error("--output requires --batch")
    if parsed.status or parsed.with_domain or parsed.merge_report:
        parser.error("--status, --with-domain, and --merge-report require --batch")
    if not parsed.targets:
        parser.print_help()
        raise SystemExit(2)
    urls = [a for a in parsed.targets if _looks_like_url(a)]
    names = [a for a in parsed.targets if not _looks_like_url(a)]
    if len(urls) > 1 or len(names) > 1:
        parser.error("provide at most one company name and one URL")
    name = names[0] if names else None

    if urls:
        entry, info = probe_url(urls[0], name=name)
        if not entry:
            print(f"❌ {info}")
            raise SystemExit(1)
        count = "?" if info is None else info
        print(f"✅ {entry['ats']} board verified ({count} live postings)\n")
        print(yaml_entry(entry))
        print(f"Add it with:\n  python3 add_source.py \"{entry['name']}\" --url \"{urls[0]}\"")
        return

    print(f"Probing slug candidates for '{name}': {', '.join(slug_candidates(name))}")
    hits = probe_name(name)
    if not hits:
        print("❌ No Greenhouse/Ashby/Lever board found.")
        print("   Try the careers page URL instead (Workday/Eightfold are URL-detected):")
        print(f"   python3 probe.py \"{name}\" https://careers.example.com")
        raise SystemExit(1)
    for hit in hits:
        print(f"✅ {hit['ats']}: slug '{hit['slug']}' ({hit['jobs']} live postings)")
    best = max(hits, key=lambda h: h["jobs"])
    print()
    print(yaml_entry({"name": name, "ats": best["ats"], "slug": best["slug"]}))
    print(f"Add it with:\n  python3 add_source.py \"{name}\"")


if __name__ == "__main__":
    main()
