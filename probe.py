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
import math
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
WORKDAY_SEARCH = "internship"
WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_PAGES = 12
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


DOMAIN_TLDS = (".com", ".ai", ".io", ".co")


def domain_candidates(name, limit=6):
    """Plausible company domains derived from a name, most likely first.

    Name-based probing only reaches the three slug ATSes, so a company on
    Workday/Workable/SmartRecruiters is invisible to it no matter how good the
    slug guess is. Trying the company's own careers page reaches every adapter
    `probe_url` understands. Kept deliberately small: this runs only after slug
    probing has already failed, and each candidate costs up to three requests
    inside `discover_careers_urls`."""
    base = re.sub(r"[^a-z0-9 ]+", "", name.lower()).strip()
    words = [w for w in base.split() if w]
    if not words:
        return []
    stems = ["".join(words)]
    trimmed = [w for w in words if w not in SUFFIX_WORDS]
    if trimmed and trimmed != words:
        stems.append("".join(trimmed))
    if len(words) > 1:
        stems.append(words[0])
    out = []
    for stem in stems:
        for tld in DOMAIN_TLDS:
            candidate = stem + tld
            if candidate not in out:
                out.append(candidate)
    return out[:limit]


def probe_derived_domains(name, limit=6):
    """Try `domain_candidates` until one yields a supported board.

    Returns ``(entry, live_count, domain, url, errors, unsupported)``; entry is
    None when nothing resolved. Transport failures are collected rather than
    raised so a flaky DNS lookup is never recorded as a definitive "not
    found". Unsupported ATS fingerprints are retained for the adapter roadmap.
    """
    errors = []
    unsupported = []
    for domain in domain_candidates(name, limit=limit):
        try:
            urls, discovery_errors = discover_careers_urls(domain)
        except ValueError:
            continue
        except requests.RequestException as exc:
            errors.append(f"{domain}: {exc}")
            continue
        errors.extend(discovery_errors)
        for url in urls[:3]:
            try:
                entry, info = probe_url(url, name=name)
            except requests.RequestException as exc:
                errors.append(f"{url}: {exc}")
                continue
            if entry:
                identity_ok, identity_evidence = check_entry_identity(name, entry)
                if identity_ok is not True:
                    errors.append(f"{url}: {identity_evidence}")
                    continue
                return entry, info, domain, url, errors, unsupported
            ats = fingerprint_unsupported_url(url)
            if ats:
                unsupported.append({
                    "ats": ats, "company_domain": domain,
                    "careers_url": url, "reason": info,
                })
    return None, None, None, None, errors, unsupported


def slug_is_high_confidence(name, slug):
    """True when a slug represents the full company name, not one generic word."""
    base = re.sub(r"[^a-z0-9 ]+", "", name.lower()).strip()
    words = base.split()
    return slug in {"".join(words), "-".join(words)}


IDENTITY_SUFFIX_WORDS = {
    "ai", "aviation", "company", "data", "energy", "group", "health",
    "holdings", "inc", "interactive", "lab", "labs", "motors", "network",
    "networks", "security", "space", "systems", "technologies", "technology",
}


def _identity_aliases(name):
    """Return the full name plus a conservative suffix-free trading name."""
    words = re.findall(r"[a-z0-9]+", name.casefold())
    aliases = [" ".join(words)] if words else []
    trimmed = list(words)
    while len(trimmed) > 1 and trimmed[-1] in IDENTITY_SUFFIX_WORDS:
        trimmed.pop()
    core = " ".join(trimmed)
    # A short, generic core (for example, Pine from Pine Labs) is too weak to
    # distinguish a legitimate trading name from a different company.
    if (len(re.sub(r"[^a-z0-9]", "", core)) >= 5
            and core and core not in aliases):
        aliases.append(core)
    return aliases


def _strong_identity_phrase(text, aliases):
    """Find a company-introduction phrase, not an arbitrary name mention."""
    plain = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    normalized = plain.casefold()
    for alias in aliases:
        escaped = r"\s+".join(map(re.escape, alias.split()))
        heading = re.compile(
            rf"^(?:about|join|meet|welcome\s+to|why)\s+{escaped}"
            rf"\s*(?:$|[|:;,.!?\-—–])")
        if any(heading.search(re.sub(r"\s+", " ", line).strip())
               for line in normalized.splitlines()):
            return alias
        sentence_patterns = (
            rf"\b(?:at)\s+{escaped}\s*[,;:—–-]",
            rf"\b{escaped}\s+(?:is|builds|creates|develops|helps|provides|was)\b",
        )
        if any(re.search(pattern, normalized) for pattern in sentence_patterns):
            return alias
    return None


TITLE_BOILERPLATE_WORDS = {
    "at", "career", "careers", "employment", "homepage", "hiring", "job",
    "jobs", "open", "opening", "openings", "opportunities", "opportunity",
    "position", "positions", "roles", "with",
}
TITLE_LEGAL_WORDS = {
    "co", "company", "corp", "corporation", "inc", "incorporated", "limited",
    "llc", "ltd", "plc", "software",
}


def _title_identity_matches(title, aliases):
    """Require a token-exact title after removing only ATS boilerplate."""
    title_words = re.findall(r"[a-z0-9]+", title.casefold())
    meaningful = [word for word in title_words if word not in TITLE_BOILERPLATE_WORDS]
    for alias in aliases:
        alias_words = alias.split()
        for start in range(len(meaningful) - len(alias_words) + 1):
            if meaningful[start:start + len(alias_words)] != alias_words:
                continue
            extras = meaningful[:start] + meaningful[start + len(alias_words):]
            if all(word in TITLE_LEGAL_WORDS for word in extras):
                return True
    return False


def _redirect_identity_matches(host, aliases):
    """Treat a company-controlled redirect host as evidence, not its path."""
    labels = [part for part in host.casefold().split(".") if part]
    normalized_labels = {
        re.sub(r"[^a-z0-9]+", "", label)
        for label in labels
        if label not in {"www", "careers", "jobs", "apply"}
    }
    return any(
        re.sub(r"[^a-z0-9]+", "", alias) in normalized_labels
        for alias in aliases
    )


def check_job_payload_identity(name, ats, slug):
    """Corroborate a generic ATS page title using structured job payloads.

    Greenhouse exposes an explicit ``company_name``. Ashby and Lever expose
    full job descriptions, where a tightly scoped company-introduction phrase
    is stronger evidence than an arbitrary occurrence of a short brand word.
    Only the first three jobs are inspected. An empty or malformed response
    stays reviewable rather than being mistaken for positive evidence.
    """
    endpoints = {
        "greenhouse": (
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            lambda payload: payload.get("jobs") if isinstance(payload, dict) else None,
        ),
        "ashby": (
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            lambda payload: payload.get("jobs") if isinstance(payload, dict) else None,
        ),
        "lever": (
            f"https://api.lever.co/v0/postings/{slug}?mode=json",
            lambda payload: payload if isinstance(payload, list) else None,
        ),
    }
    endpoint = endpoints.get(ats)
    if not endpoint:
        return None, f"job-payload identity checking is not implemented for {ats}"
    try:
        response = requests.get(endpoint[0], timeout=TIMEOUT, headers=HEADERS)
    except requests.RequestException as exc:
        return None, f"job-payload identity check failed: {exc}"
    if response.status_code != 200:
        return None, (
            f"job-payload identity check returned HTTP {response.status_code}")
    payload = _json_or_none(response)
    jobs = endpoint[1](payload)
    if not isinstance(jobs, list) or not jobs:
        return None, "job-payload identity check returned no inspectable jobs"

    aliases = _identity_aliases(name)
    normalized_aliases = {
        re.sub(r"[^a-z0-9]+", "", alias) for alias in aliases if alias
    }
    for job in jobs[:3]:
        if not isinstance(job, dict):
            continue
        company_name = job.get("company_name")
        normalized_company = re.sub(
            r"[^a-z0-9]+", "", str(company_name or "").casefold())
        if normalized_company and normalized_company in normalized_aliases:
            return True, f"job payload company: {company_name}"
        text = " ".join(str(job.get(field) or "") for field in (
            "descriptionHtml", "descriptionPlain", "description", "openingPlain",
            "opening", "additionalPlain", "additional", "content",
        ))
        phrase = _strong_identity_phrase(text, aliases)
        if phrase:
            return True, f"job payload introduction: {phrase}"
    return False, f"job payload does not identify {name!r}"


def check_board_identity(name, ats, slug):
    """Confirm a slug board's visible identity before calling it verified.

    Exact-looking slugs still collide: ``bcg`` is Bohen Consulting Group and
    ``tcs`` is Thornbury Community Services. The public board title or its
    official redirect must contain the intended company name. ``None`` means
    the identity check itself was unreliable and therefore still needs review.
    """
    bases = {
        "greenhouse": "https://job-boards.greenhouse.io/",
        "lever": "https://jobs.lever.co/",
        "ashby": "https://jobs.ashbyhq.com/",
    }
    base = bases.get(ats)
    if not base:
        return None, f"identity checking is not implemented for {ats}"
    aliases = _identity_aliases(name)
    board_host = (urlparse(base).hostname or "").casefold()
    title = ""
    response_url = base + slug
    page_issue = None
    try:
        response = requests.get(
            response_url, timeout=TIMEOUT,
            headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
            allow_redirects=True)
    except requests.RequestException as exc:
        response = None
        page_issue = f"board identity check failed: {exc}"
    if response is not None and response.status_code == 200:
        response_url = response.url
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        final_host = (urlparse(response.url).hostname or "").casefold()
        # The original ATS URL contains the slug under test, so counting that
        # path as evidence would make every collision pass. A redirect is only
        # evidence when it leaves the ATS host for a company-controlled site.
        redirect_matches = (
            final_host != board_host
            and _redirect_identity_matches(final_host, aliases)
        )
        if _title_identity_matches(title, aliases) or redirect_matches:
            return True, title or response.url
    elif response is not None:
        page_issue = f"board identity check returned HTTP {response.status_code}"

    # Public landing pages are sometimes 403-protected while their documented
    # job APIs remain healthy. Always attempt the structured payload before
    # declaring identity unverifiable.
    payload_ok, payload_evidence = check_job_payload_identity(name, ats, slug)
    if payload_ok is True:
        return True, payload_evidence
    page_evidence = page_issue or (
        f"board title/redirect does not identify {name!r}: "
        f"{title or response_url!r}")
    if payload_ok is None:
        return None, f"{page_evidence}; {payload_evidence}"
    return False, f"{page_evidence}; {payload_evidence}"


def check_entry_identity(name, entry):
    """Apply slug-board identity checks to a discovered adapter entry."""
    if entry.get("ats") in {"greenhouse", "ashby", "lever"} and entry.get("slug"):
        return check_board_identity(name, entry["ats"], entry["slug"])
    return True, None


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
    payload = _json_or_none(res)
    if (not isinstance(payload, dict) or "totalFound" not in payload
            or not isinstance(payload.get("content"), list)):
        return None
    try:
        return int(payload["totalFound"])
    except (TypeError, ValueError):
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


def check_workday(tenant, wd_host, site, search=WORKDAY_SEARCH):
    """Verify a Workday CXS board with the same POST the hunter uses."""
    url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    res = requests.post(url, json={"limit": 1, "offset": 0, "searchText": search},
                        timeout=TIMEOUT,
                        headers={**HEADERS, "Content-Type": "application/json"})
    if res.status_code != 200:
        return None
    payload = _json_or_none(res)
    if (isinstance(payload, dict) and "total" in payload
            and isinstance(payload.get("jobPostings"), list)):
        try:
            total = int(payload["total"])
        except (TypeError, ValueError):
            return None
        return total if total >= 0 else None
    return None


def workday_entry(name, tenant, wd_host, site, search=WORKDAY_SEARCH):
    """Build a Workday entry only when its complete result set is readable."""
    count = check_workday(tenant, wd_host, site, search=search)
    if count is None:
        return None, (
            f"Workday CXS verify failed (tenant={tenant}, wd_host={wd_host}, "
            f"site={site}, search={search!r})")
    pages = max(1, math.ceil(count / WORKDAY_PAGE_SIZE))
    if pages > WORKDAY_MAX_PAGES:
        return None, (
            f"Workday search {search!r} returns {count} postings and needs "
            f"{pages} pages, above the {WORKDAY_MAX_PAGES}-request ceiling")
    entry = {
        "name": name, "ats": "workday", "tenant": tenant,
        "wd_host": wd_host, "site": site, "search": search,
    }
    if pages > 4:
        entry["max_pages"] = pages
    return entry, count


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
        return workday_entry(name or tenant, tenant, wd_host, site)

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


def probe_candidate(candidate, use_derived_domains=True):
    """Probe one ledger row and return review data without mutating the row."""
    result = dict(candidate)
    for field in (
            "probe_status", "last_probed_at", "reason", "warnings",
            "live_postings", "suggested_entry", "alternatives", "unsupported_ats",
            "discovered_careers_url", "identity_evidence"):
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
        identity_mismatches = []
        for discovered_url in discovered[:3]:
            try:
                entry, info = probe_url(discovered_url, name=name)
            except requests.RequestException as exc:
                discovery_errors.append(f"{discovered_url}: {exc}")
                continue
            if entry:
                identity_ok, identity_evidence = check_entry_identity(name, entry)
                if identity_ok is not True:
                    identity_mismatches.append(
                        f"{discovered_url}: {identity_evidence}")
                    continue
                verified.append((discovered_url, entry, info, identity_evidence))
            else:
                unsupported.append((discovered_url, info))
        if verified:
            deduped = {}
            for item in verified:
                key = yaml.safe_dump(item[1], sort_keys=True)
                deduped.setdefault(key, item)
            verified = list(deduped.values())
            discovered_url, entry, info, identity_evidence = verified[0]
            result.update({
                "status": "probed",
                "probe_status": (
                    "needs_review" if len(verified) > 1 else "verified_endpoint"),
                "careers_url": discovered_url,
                "discovered_careers_url": discovered_url,
                "live_postings": info,
                "suggested_entry": entry,
            })
            if identity_evidence:
                result["identity_evidence"] = identity_evidence
            if len(verified) > 1:
                result["alternatives"] = [
                    {"careers_url": item[0], "suggested_entry": item[1],
                     "live_postings": item[2]}
                    for item in verified
                ]
            return result
        if identity_mismatches and not unsupported:
            result.update({
                "status": "probed",
                "probe_status": "needs_review",
                "reason": "discovered ATS links failed company identity checks",
                "warnings": identity_mismatches[:3],
            })
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
        if identity_mismatches:
            result["warnings"] = identity_mismatches[:3]
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
        identity_ok, identity_evidence = check_entry_identity(name, entry)
        if identity_ok is not True:
            result.update({
                "status": "probed",
                "probe_status": "needs_review",
                "live_postings": info,
                "suggested_entry": entry,
                "warnings": [identity_evidence],
            })
            return result
        result.update({
            "status": "probed",
            "probe_status": "verified_endpoint",
            "live_postings": info,
            "suggested_entry": entry,
        })
        if identity_evidence:
            result["identity_evidence"] = identity_evidence
        return result

    hits, errors = probe_name_detailed(name)
    unique_hits = {
        (hit["ats"], hit["slug"]): hit
        for hit in hits
    }
    hits = list(unique_hits.values())
    if not hits and use_derived_domains:
        # Slug probing only reaches Greenhouse/Ashby/Lever. Before calling this
        # a miss, try the company's own careers page, which reaches every
        # adapter probe_url understands — that is how Hugging Face (Workable)
        # and BrowserStack (Workday) turn up.
        entry, info, domain, url, domain_errors, unsupported = probe_derived_domains(name)
        if entry:
            result.update({
                "status": "probed",
                "probe_status": "verified_endpoint",
                "company_domain": domain,
                "careers_url": url,
                "discovered_careers_url": url,
                "live_postings": info,
                "suggested_entry": entry,
            })
            return result
        if unsupported:
            blocked = unsupported[0]
            result.update({
                "status": "probed",
                "probe_status": "unsupported",
                "company_domain": blocked["company_domain"],
                "careers_url": blocked["careers_url"],
                "discovered_careers_url": blocked["careers_url"],
                "unsupported_ats": blocked["ats"],
                "reason": blocked["reason"],
            })
            if domain_errors:
                result["warnings"] = [
                    f"derived-domain probe: {error}"
                    for error in domain_errors[:2]
                ]
            return result
        # Deliberately NOT folded into the failure decision: derived domains are
        # guesses, so a guess that does not resolve is an expected miss, not an
        # unreliable probe. Only the slug probe's own transport errors — against
        # endpoints known to exist — can downgrade this to "failed".
        if domain_errors:
            result["warnings"] = (result.get("warnings") or []) + [
                f"derived-domain probe: {e}" for e in domain_errors[:2]]
        if errors:
            result.update({
                "probe_status": "failed",
                "reason": "all or part of the probe was unreliable: " + "; ".join(errors[:3]),
            })
        else:
            result.update({
                "status": "probed",
                "probe_status": "not_found",
                "reason": ("no Greenhouse, Ashby, or Lever board matched the name, "
                           "and no supported board was found at its derived domains"),
            })
        return result

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
    lossy = not slug_is_high_confidence(name, best["slug"])
    identity_ok = None
    identity_evidence = None
    if len(ranked) == 1:
        identity_ok, identity_evidence = check_board_identity(
            name, best["ats"], best["slug"])
    result.update({
        "status": "probed",
        "probe_status": (
            "needs_review"
            if len(ranked) > 1 or identity_ok is not True
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
    if lossy and identity_ok is not True:
        result.setdefault("warnings", []).append(
            f"lossy slug '{best['slug']}' does not represent the full company name")
    elif identity_ok is not True:
        result.setdefault("warnings", []).append(identity_evidence)
    else:
        result["identity_evidence"] = identity_evidence
    return result


def batch_probe(path, workers=4, statuses=None, with_domain=False,
                use_derived_domains=True):
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
            executor.submit(
                probe_candidate, candidate,
                use_derived_domains=use_derived_domains): index
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


def merge_probe_report(ledger_path, report_path, append_new=False):
    """Merge review evidence into a YAML ledger without touching config/state.

    ``append_new`` imports report rows that are not already in the ledger. It is
    intentionally opt-in so a routine re-probe cannot expand the candidate set
    by surprise.
    """
    if os.path.splitext(ledger_path)[1].lower() not in (".yaml", ".yml"):
        raise ValueError("--merge-report requires a YAML ledger")
    candidates = load_candidates(ledger_path)
    with open(report_path, encoding="utf-8") as stream:
        report = yaml.safe_load(stream)
    results = report.get("candidates") if isinstance(report, dict) else None
    if not isinstance(results, list):
        raise ValueError("probe report must contain candidates[]")
    by_name = {}
    for index, row in enumerate(results, 1):
        if not isinstance(row, dict):
            raise ValueError(f"probe report candidate {index} must be a mapping")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"probe report candidate {index} is missing a name")
        key = name.strip().casefold()
        if key in by_name:
            raise ValueError(f"duplicate candidate name in probe report: {name.strip()}")
        status = row.get("status", "candidate")
        if status not in SUPPORTED_LEDGER_STATUSES:
            raise ValueError(
                f"{name.strip()}: status must be one of "
                f"{', '.join(sorted(SUPPORTED_LEDGER_STATUSES))}")
        normalized = dict(row)
        normalized["name"] = name.strip()
        normalized["status"] = status
        by_name[key] = normalized
    fields = (
        "probe_status", "last_probed_at", "reason", "warnings",
        "live_postings", "suggested_entry", "alternatives", "unsupported_ats",
        "company_domain", "careers_url", "discovered_careers_url",
        "identity_evidence",
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
            elif field != "company_domain":
                candidate.pop(field, None)
    if append_new:
        known_names = {candidate["name"].casefold() for candidate in candidates}
        for key, result in by_name.items():
            if key in known_names:
                continue
            candidates.append(dict(result))
            known_names.add(key)
            merged += 1
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


def audit_config_identities(path="config.yaml", workers=4):
    """Live-check every configured slug board for company-identity drift."""
    if not 1 <= workers <= 16:
        raise ValueError("--workers must be between 1 and 16")
    with open(path, encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    entries = config.get("ats_companies")
    if not isinstance(entries, list):
        raise ValueError("config ats_companies must be a list")
    targets = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"config ats_companies entry {index + 1} must be a mapping")
        if entry.get("ats") not in {"greenhouse", "ashby", "lever"}:
            continue
        name, slug = entry.get("name"), entry.get("slug")
        if not isinstance(name, str) or not isinstance(slug, str):
            raise ValueError(
                f"slug-based source at entry {index + 1} requires name and slug")
        identity_aliases = entry.get("identity_aliases", [])
        if (not isinstance(identity_aliases, list)
                or any(not isinstance(alias, str) or not alias.strip()
                       for alias in identity_aliases)):
            raise ValueError(
                f"{name}: identity_aliases must be a list of non-empty strings")
        targets.append((index, name, entry["ats"], slug, identity_aliases))

    def inspect(target):
        index, name, ats, slug, identity_aliases = target
        try:
            matched, evidence = check_board_identity(name, ats, slug)
            for alias in identity_aliases:
                if matched is True:
                    break
                alias_matched, alias_evidence = check_board_identity(alias, ats, slug)
                if alias_matched is True:
                    matched = True
                    evidence = (
                        f"configured identity alias {alias!r}: {alias_evidence}")
                    break
        except Exception as exc:  # keep one unexpected probe from hiding the rest
            matched, evidence = None, f"identity audit raised {type(exc).__name__}: {exc}"
        return {
            "index": index,
            "name": name,
            "ats": ats,
            "slug": slug,
            "status": (
                "verified" if matched is True
                else "mismatch" if matched is False
                else "unverifiable"
            ),
            "evidence": evidence,
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(inspect, targets))
    return sorted(results, key=lambda result: result["index"])


def print_identity_audit(results):
    counts = {status: 0 for status in ("verified", "mismatch", "unverifiable")}
    for result in results:
        counts[result["status"]] += 1
    print(
        f"Audited {len(results)} configured slug source(s): "
        f"{counts['verified']} verified, {counts['mismatch']} mismatch, "
        f"{counts['unverifiable']} unverifiable")
    for result in results:
        if result["status"] == "verified":
            continue
        marker = "❌" if result["status"] == "mismatch" else "⚠️"
        print(
            f"{marker} {result['name']} ({result['ats']}:{result['slug']}): "
            f"{result['evidence']}")


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
    parser.add_argument("--slug-only", action="store_true",
                        help="With --batch, skip derived-domain fallback after slug misses")
    parser.add_argument("--merge-report", metavar="REPORT",
                        help="Merge an existing report into the YAML --batch ledger and exit")
    parser.add_argument("--append-new", action="store_true",
                        help="With --merge-report, also import report rows absent from the ledger")
    parser.add_argument(
        "--audit-config-identities", nargs="?", const="config.yaml", metavar="PATH",
        help="Live-check every configured Greenhouse/Ashby/Lever board for identity drift")
    parsed = parser.parse_args()

    if parsed.audit_config_identities:
        if (parsed.targets or parsed.batch or parsed.output or parsed.status
                or parsed.with_domain or parsed.slug_only or parsed.merge_report
                or parsed.append_new):
            parser.error(
                "--audit-config-identities cannot be combined with probing options")
        try:
            results = audit_config_identities(
                parsed.audit_config_identities, workers=parsed.workers)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"❌ {exc}") from exc
        print_identity_audit(results)
        raise SystemExit(
            1 if any(result["status"] != "verified" for result in results) else 0)

    if parsed.batch:
        if parsed.targets:
            parser.error("positional targets cannot be combined with --batch")
        try:
            if parsed.merge_report:
                if parsed.output or parsed.status or parsed.with_domain or parsed.slug_only:
                    parser.error(
                        "--merge-report cannot be combined with probe selection options")
                merged = merge_probe_report(
                    parsed.batch, parsed.merge_report, append_new=parsed.append_new)
                print(f"Merged probe evidence for {merged} candidate(s) into {parsed.batch}")
                return
            if parsed.append_new:
                parser.error("--append-new requires --merge-report")
            report = batch_probe(
                parsed.batch, parsed.workers, statuses=parsed.status,
                with_domain=parsed.with_domain,
                use_derived_domains=not parsed.slug_only)
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
    if (parsed.status or parsed.with_domain or parsed.slug_only or parsed.merge_report
            or parsed.append_new):
        parser.error(
            "--status, --with-domain, --slug-only, --merge-report, and --append-new "
            "require --batch")
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
