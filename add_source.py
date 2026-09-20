"""Add a company to tracking in one command: probe → append to config.yaml →
verify with --test → baseline with --seed.

Usage:
    python3 add_source.py "Company Name"
    python3 add_source.py "Company Name" --url https://careers.example.com
    python3 add_source.py "Company Name" --ats greenhouse --slug example
    python3 add_source.py "Company Name" --ats workday --tenant nvidia \\
        --site NVIDIAExternalCareerSite [--wd-host wd5]
    python3 add_source.py "Company Name" --ats eightfold \\
        --base-url https://careers.example.com --domain example.com
    python3 add_source.py --batch probe-report.yaml          # review only
    python3 add_source.py --batch probe-report.yaml --apply  # approved rows only

The config edit is rolled back automatically if the --test verification run
fails. --no-seed skips baselining (first live run will then alert on every
currently-open match). Batch mode is safe by default: it only shows commands.
--apply processes rows explicitly marked ``approved: true`` and re-runs every
live verification before editing config. Empty boards are rejected unless
--allow-empty is explicitly supplied. Custom (JS-only) pages are out of
scope — see .agents/skills/add-source for that decision tree.
"""
import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

import yaml

import probe

CONFIG_FILE = "config.yaml"
STATE_FILE = "state.json"


def build_explicit_entry(args, verify_identity=False):
    """Build and live-verify an entry from explicit --ats flags."""
    name, ats = args.name, args.ats
    if args.field:
        try:
            import hybrid_hunter
            known = hybrid_hunter.KNOWN_ATS_TYPES
            required = hybrid_hunter.REQUIRED_ATS_FIELDS.get(ats)
        except ImportError:
            known = set(probe.SLUG_CHECKERS and ("greenhouse", "ashby", "lever"))
            required = None
        if ats not in known:
            raise SystemExit(f"--ats {ats} isn't a known adapter")
        fields = {}
        for item in args.field:
            if "=" not in item:
                raise SystemExit(f"--field must use key=value (got {item!r})")
            key, value = item.split("=", 1)
            key = key.strip()
            if not key or not value.strip():
                raise SystemExit(f"--field must use a non-empty key and value (got {item!r})")
            fields[key] = value
        missing = [field for field in (required or ()) if not fields.get(field)]
        if missing:
            raise SystemExit(
                f"--ats {ats} requires --field " + ", --field ".join(missing))
        entry = {"name": name, "ats": ats, **fields}
        # Generic fields are intentionally verbatim; the post-insert --test
        # is the authoritative structural/live verification step.
        if verify_identity:
            identity_ok, evidence = probe.check_entry_identity(name, entry)
            if identity_ok is not True:
                raise SystemExit(f"❌ Explicit fields did not verify {name!r}: {evidence}")
        return entry, None
    if ats in ("greenhouse", "ashby", "lever"):
        if not args.slug:
            raise SystemExit(f"--ats {ats} requires --slug")
        checker = dict(probe.SLUG_CHECKERS)[ats]
        count = checker(args.slug)
        if count is None:
            raise SystemExit(f"❌ {ats} API rejected slug '{args.slug}' — not adding.")
        return {"name": name, "ats": ats, "slug": args.slug}, count
    if ats == "smartrecruiters":
        if not args.company_id:
            raise SystemExit("--ats smartrecruiters requires --company-id")
        first_query = args.query[0] if args.query else None
        count = probe.check_smartrecruiters(args.company_id, query=first_query)
        if count is None:
            raise SystemExit(
                f"❌ SmartRecruiters API rejected company id '{args.company_id}' — not adding.")
        entry = {
            "name": name, "ats": "smartrecruiters", "company_id": args.company_id,
        }
        if args.country:
            entry["country"] = args.country
        if args.query:
            if len(args.query) == 1:
                entry["query"] = args.query[0]
            else:
                entry["queries"] = args.query
        return entry, count
    if ats == "workable":
        if not args.account:
            raise SystemExit("--ats workable requires --account")
        count = probe.check_workable(args.account)
        if count is None:
            raise SystemExit(
                f"❌ Workable public API rejected account '{args.account}' — not adding.")
        return {"name": name, "ats": "workable", "account": args.account}, count
    if ats == "workday":
        if not (args.tenant and args.site):
            raise SystemExit("--ats workday requires --tenant and --site")
        search = args.search or probe.WORKDAY_SEARCH
        entry, info = probe.workday_entry(
            name, args.tenant, args.wd_host, args.site, search=search)
        if entry is None:
            raise SystemExit(f"❌ {info} — not adding.")
        if args.max_pages is not None:
            required_pages = max(
                1, (info + probe.WORKDAY_PAGE_SIZE - 1) // probe.WORKDAY_PAGE_SIZE)
            if not 1 <= args.max_pages <= probe.WORKDAY_MAX_PAGES:
                raise SystemExit(
                    f"--max-pages must be from 1 to {probe.WORKDAY_MAX_PAGES}")
            if args.max_pages < required_pages:
                raise SystemExit(
                    f"❌ --max-pages {args.max_pages} cannot read all {info} postings; "
                    f"at least {required_pages} page(s) are required.")
            entry["max_pages"] = args.max_pages
        return entry, info
    if ats == "eightfold":
        if not (args.base_url and args.domain):
            raise SystemExit("--ats eightfold requires --base-url and --domain")
        count = probe.check_eightfold(args.base_url, args.domain)
        if count is None:
            raise SystemExit(f"❌ Eightfold verify failed for {args.base_url} — not adding.")
        return {"name": name, "ats": "eightfold", "base_url": args.base_url,
                "domain": args.domain, "query": "intern", "location": "India"}, count
    raise SystemExit(f"--ats {ats} isn't supported by add_source.py; add the "
                     f"entry to config.yaml by hand (see README's ATS table).")


def resolve_entry(args):
    """Returns (entry, live_count_or_None)."""
    if getattr(args, "from_job_url", None):
        parsed = probe.parse_job_url(args.from_job_url)
        if not parsed:
            # Preserve --url semantics for custom/unknown job URLs: the
            # regular careers-page probe may still recognize Eightfold or
            # JSON-LD even when the concrete URL has no stable pattern.
            entry, info = probe.probe_url(args.from_job_url, name=args.name)
            if not entry:
                raise SystemExit(f"❌ {info}")
            return entry, info
        parsed["name"] = args.name
        # A concrete posting is only an identifier hint.  Return an unknown
        # count so the normal post-insert --test remains the source of truth.
        return parsed, None
    if args.ats:
        entry, count = build_explicit_entry(args, verify_identity=False)
        identity_ok, evidence = probe.check_entry_identity(name, entry)
        if identity_ok is not True:
            raise SystemExit(f"❌ Explicit fields did not verify {name!r}: {evidence}")
        return entry, count
    if args.url:
        entry, info = probe.probe_url(args.url, name=args.name)
        if not entry:
            raise SystemExit(f"❌ {info}")
        identity_ok, identity_evidence = probe.check_entry_identity(args.name, entry)
        if identity_ok is not True:
            raise SystemExit(
                "❌ The discovered board does not identify the intended company: "
                f"{identity_evidence}")
        if entry.get("ats") == "workday" and (args.search or args.max_pages is not None):
            search = args.search or entry["search"]
            entry, info = probe.workday_entry(
                args.name, entry["tenant"], entry["wd_host"], entry["site"],
                search=search)
            if entry is None:
                raise SystemExit(f"❌ {info} — not adding.")
            if args.max_pages is not None:
                required_pages = max(
                    1, (info + probe.WORKDAY_PAGE_SIZE - 1) // probe.WORKDAY_PAGE_SIZE)
                if not 1 <= args.max_pages <= probe.WORKDAY_MAX_PAGES:
                    raise SystemExit(
                        f"--max-pages must be from 1 to {probe.WORKDAY_MAX_PAGES}")
                if args.max_pages < required_pages:
                    raise SystemExit(
                        f"❌ --max-pages {args.max_pages} cannot read all {info} postings; "
                        f"at least {required_pages} page(s) are required.")
                entry["max_pages"] = args.max_pages
        return entry, info
    print(f"Probing slug candidates: {', '.join(probe.slug_candidates(args.name))}")
    hits, slug_errors = probe.probe_name_detailed(args.name)
    if not hits:
        # Slug probing only covers three ATSes.  A single-name onboarding
        # attempt should still discover Workday/Workable/SmartRecruiters by
        # trying a small set of company-domain guesses.  Domain misses are a
        # clean not-found result; probe_derived_domains deliberately returns
        # transport errors separately so a flaky DNS lookup is not presented
        # as evidence that the company has no board.
        entry, info, domain, discovered_url, errors, unsupported = (
            probe.probe_derived_domains(args.name))
        if entry:
            print(f"✅ Discovered {entry['ats']} board at {discovered_url} "
                  f"via {domain} ({info} live postings).")
            return entry, info
        if any("ambiguous derived-domain" in error for error in errors):
            raise SystemExit(
                "❌ Derived-domain probing found multiple supported boards; "
                "refusing to guess. Use --url or explicit --ats fields.")
        detail = ""
        if unsupported:
            detail = (" Known unsupported ATS fingerprints were found; add the "
                      "adapter manually or use a custom page.")
        raise SystemExit(
            "❌ No supported ATS board found for that name after slug and "
            "derived-domain probing.\n"
            "   Retry with the careers page URL (detects Workday/Eightfold too):\n"
            f"   python3 add_source.py \"{args.name}\" --url https://...\n"
            "   JS-only board or program page? See .agents/skills/add-source."
            + detail)
    best = max(hits, key=lambda h: h["jobs"])
    if len(hits) > 1 or not probe.slug_is_high_confidence(args.name, best["slug"]):
        for hit in hits:
            print(f"   found {hit['ats']}: '{hit['slug']}' ({hit['jobs']} postings)")
        raise SystemExit(
            "❌ Name-based probing is ambiguous or matched a shortened slug; "
            "refusing to guess the company identity. Review the hits, then use "
            f"`--ats {best['ats']} --slug {best['slug']}` only after confirming "
            "the board belongs to the intended company.")
    identity_ok, identity_evidence = probe.check_board_identity(
        args.name, best["ats"], best["slug"])
    if identity_ok is not True:
        raise SystemExit(
            "❌ The board endpoint exists, but its identity could not be confirmed: "
            f"{identity_evidence}. Review it, then use `--ats {best['ats']} "
            f"--slug {best['slug']}` only if it belongs to the intended company.")
    return {"name": args.name, "ats": best["ats"], "slug": best["slug"]}, best["jobs"]


def insert_entry(config_text, entry, comment=None):
    """Insert the entry at the end of ats_companies — i.e. just before the
    top-level custom_pages key — preserving all comments in the file."""
    entry_yaml = probe.yaml_entry(entry)
    if comment:
        clean_comment = " ".join(str(comment).splitlines()).strip()
        entry_yaml = f"# {clean_comment}\n" + entry_yaml
    lines = config_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("custom_pages:"):
            at = i - 1 if i > 0 and lines[i - 1].strip() == "" else i
            return "".join(lines[:at]) + entry_yaml + "".join(lines[at:])
    # No custom_pages key: ats_companies runs to EOF.
    return config_text + ("" if config_text.endswith("\n") else "\n") + entry_yaml


def run_hunter(flags, name):
    proc = subprocess.run([sys.executable, "hybrid_hunter.py", *flags, "--company", name],
                          capture_output=True, text=True)
    ok = proc.returncode == 0 and "❌" not in proc.stdout
    return ok, proc


def _read_bytes(path):
    try:
        with open(path, "rb") as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def _atomic_write(path, data, expected=None, backup_suffix=".bak"):
    """Atomically write a file, refusing to overwrite a concurrent edit."""
    current = _read_bytes(path)
    if expected is not None and current != expected:
        raise RuntimeError(f"concurrent change detected in {path}; refusing overwrite")
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    if current is not None:
        backup = path + backup_suffix
        # Keep the most recent recoverable copy without exposing a partial file.
        fd, temp_backup = tempfile.mkstemp(prefix=".backup-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(current)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_backup, backup)
        finally:
            if os.path.exists(temp_backup):
                os.unlink(temp_backup)
    fd, temp_path = tempfile.mkstemp(prefix=".atomic-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _restore_bytes(path, original, expected):
    """Restore transaction input while preserving a failed current version."""
    current = _read_bytes(path)
    if current != expected:
        # A human/CI edit after the failed operation wins; never overwrite it.
        raise RuntimeError(f"concurrent change detected while rolling back {path}")
    if original is None:
        if current is not None:
            failed = path + ".failed"
            _atomic_write(failed, current, expected=_read_bytes(failed), backup_suffix=".failed.bak")
            os.unlink(path)
        return
    _atomic_write(path, original, expected=current)


def _entry_target(entry):
    """Adapter-specific board target, excluding human name and query scope."""
    if not isinstance(entry, dict):
        return None
    ats = entry.get("ats")
    fields = {
        "greenhouse": ("slug",), "ashby": ("slug",), "lever": ("slug",),
        "workday": ("tenant", "wd_host", "site"),
        "smartrecruiters": ("company_id",), "workable": ("account",),
        "eightfold": ("base_url", "domain"), "jsonld": ("url",),
        "oracle_hcm": ("host", "site_number"),
    }.get(ats)
    if fields:
        return (ats, *(str(entry.get(field) or "").casefold() for field in fields))
    # For global adapters there is no per-company board target; keep the ATS
    # and stable identifying fields so a second scope is reviewed explicitly.
    return (ats, tuple(sorted((key, str(value).casefold()) for key, value in entry.items()
                              if key not in {"name", "keywords"})))


def _entry_scope(entry):
    if not isinstance(entry, dict):
        return ()
    scope_keys = ("query", "queries", "search", "keyword", "location", "country",
                  "country_code", "categories", "seniority", "include_multi_location",
                  "location_id", "max_pages")
    return tuple((key, repr(entry.get(key))) for key in scope_keys if key in entry)


def duplicate_status(entry, config):
    """Return exact duplicate, same target/different scope, or no duplicate.

    Aliases are treated as names for human matching, while target and scope are
    compared structurally so two Workday searches cannot be auto-merged.
    """
    entries = list(config.get("ats_companies") or []) + list(config.get("custom_pages") or [])
    target, scope = _entry_target(entry), _entry_scope(entry)
    names = {str(entry.get("name", "")).casefold(),
             *(str(alias).casefold() for alias in entry.get("aliases") or [])}
    exact = []
    target_hits = []
    for existing in entries:
        if not isinstance(existing, dict):
            continue
        existing_names = {str(existing.get("name", "")).casefold(),
                          *(str(alias).casefold() for alias in existing.get("aliases") or [])}
        same_target = target is not None and _entry_target(existing) == target
        same_scope = _entry_scope(existing) == scope
        if same_target:
            target_hits.append(existing)
        # The adapter target plus scope is authoritative.  Aliases improve
        # human-facing matching but a renamed source pointing at the exact
        # same board must still be treated as already tracked.
        if same_target and same_scope:
            exact.append(existing)
    if exact:
        return {"status": "exact_match", "matches": [e.get("name") for e in exact]}
    if target_hits:
        return {"status": "different_scope_review_required",
                "matches": [e.get("name") for e in target_hits]}
    return {"status": "new", "matches": []}


def _classify_preview_job(job, config):
    try:
        import hybrid_hunter as hh
        classifier = getattr(hh, "classify_role", None)
        if classifier is None:
            return {"verdict": "unknown", "reason": "classify_role is unavailable",
                    "evidence": []}
        value = classifier(job, config)
        if not isinstance(value, dict) or not isinstance(value.get("verdict"), str):
            return {"verdict": "unknown", "reason": "invalid classify_role result", "evidence": []}
        return {"verdict": value["verdict"],
                "reason": str(value.get("reason", "")),
                "evidence": list(value.get("evidence") or [])}
    except Exception as exc:
        return {"verdict": "unknown", "reason": f"classification failed: {exc}", "evidence": []}


def preview_source(args):
    """Probe and read one proposed ATS entry without touching config/state."""
    import hybrid_hunter as hh
    config_bytes = _read_bytes(CONFIG_FILE)
    if config_bytes is None:
        raise SystemExit(f"❌ Could not read {CONFIG_FILE}")
    try:
        config = yaml.safe_load(config_bytes.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"❌ Could not parse {CONFIG_FILE}: {exc}") from exc
    name = args.name.strip() if isinstance(args.name, str) and args.name.strip() else None
    source_url = args.url or args.from_job_url
    # A URL-only preview may discover a board, but its slug/account is not a
    # company name. Keep it as a resumable draft unless a caller supplied name.
    if source_url:
        try:
            entry, info = probe.probe_url(source_url, name=name)
        except Exception as exc:
            entry, info = None, f"probe failed: {exc}"
        if not entry:
            row = {"name": name or "", "status": "candidate", "probe_status": "needs_review",
                   "actionable": False, "missing_fields": (["name"] if not name else []),
                   "next_action": "Provide a verified careers URL or supported ATS fields",
                   "evidence": [str(info)]}
            report = {"version": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "source": source_url, "summary": {"total": 1, "actionable": 0},
                      "candidates": [row]}
            return report
    else:
        try:
            entry, info = resolve_entry(args)
        except SystemExit as exc:
            row = {"name": name or "", "status": "candidate", "probe_status": "needs_review",
                   "actionable": False, "missing_fields": (["name"] if not name else []),
                   "next_action": "Resolve the ambiguity with --url or reviewed ATS fields",
                   "evidence": [str(exc)]}
            return {"version": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "source": name or "preview", "summary": {"total": 1, "actionable": 0},
                    "candidates": [row]}
    if not name:
        row = {"name": "", "status": "candidate", "probe_status": "needs_review",
               "suggested_entry": entry, "identity_evidence": [], "actionable": False,
               "missing_fields": ["name"], "next_action": "Re-run with the intended company name",
               "evidence": ["URL recognized, but its ATS identifier is not a verified company name"]}
        return {"version": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": source_url or "preview", "summary": {"total": 1, "actionable": 0},
                "candidates": [row]}

    try:
        identity_ok, identity_evidence = probe.check_entry_identity(name, entry)
    except Exception as exc:
        identity_ok, identity_evidence = None, f"identity check failed: {exc}"
    entry = dict(entry)
    entry["name"] = name
    proposed = copy.deepcopy(config)
    proposed.setdefault("ats_companies", []).append(entry)
    duplicate = duplicate_status(entry, config)
    row = {"name": name, "status": "probed", "probe_status": "verified_endpoint" if identity_ok is True else "needs_review",
           "suggested_entry": entry, "identity_evidence": [identity_evidence] if identity_evidence else [],
           "duplicate_status": duplicate["status"], "duplicate_matches": duplicate["matches"],
           "raw_count": None, "eligible_count": 0, "relevance_counts": {}, "examples": {},
           "actionable": identity_ok is True and duplicate["status"] == "new"}
    if identity_ok is not True:
        row.update({"actionable": False, "missing_fields": ["identity_evidence"],
                    "next_action": "Confirm the official company identity before applying"})
        return {"version": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": source_url or name, "summary": {"total": 1, "actionable": 0},
                "candidates": [row]}
    semaphores = defaultdict(lambda: __import__("threading").Semaphore(1))
    try:
        result = hh._hunt_ats_entry(entry, proposed, semaphores)
    except Exception as exc:
        result = {"matches": [], "error": str(exc), "raw_count": None}
    row["raw_count"] = result.get("raw_count")
    if result.get("error"):
        row.update({"probe_status": "failed", "actionable": False,
                    "missing_fields": ["successful_full_read"],
                    "next_action": "Retry preview after the board is readable",
                    "evidence": [str(result["error"])]})
    else:
        counts = {}
        examples = {}
        for job in result.get("matches") or []:
            classified = _classify_preview_job(job, proposed)
            verdict = classified["verdict"]
            counts[verdict] = counts.get(verdict, 0) + 1
            examples.setdefault(verdict, [])
            if len(examples[verdict]) < 5:
                examples[verdict].append({"id": job.get("id"), "title": job.get("title"),
                                          "location": job.get("location"), "url": job.get("url"),
                                          "reason": classified["reason"], "evidence": classified["evidence"]})
        row["relevance_counts"] = counts
        # _hunt_ats_entry already applies the production keyword/location
        # filters; classification is a separate, mode-independent review
        # layer and must not redefine this count.
        row["eligible_count"] = len(result.get("matches") or [])
        row["examples"] = examples
        row["evidence"] = [f"full ATS read returned {len(result.get('matches') or [])} eligible-filter matches"]
        if duplicate["status"] != "new":
            row.update({"actionable": False, "missing_fields": ["duplicate_review"],
                        "next_action": "Review existing source target/scope before applying"})
    report = {"version": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "source": source_url or name, "summary": {"total": 1, "actionable": int(row["actionable"])},
              "candidates": [row]}
    return report


def batch_command(candidate, no_seed=False, allow_empty=False):
    """Build a single-source command for a reviewed batch candidate."""
    if candidate.get("approved") is not True:
        return None, "not approved"
    if candidate.get("actionable") is False:
        return None, candidate.get("next_action") or "preview row is non-actionable"
    if candidate.get("duplicate_status") not in (None, "new"):
        return None, "preview found a duplicate target/scope; review required"
    if candidate.get("probe_status") in {"needs_review", "failed", "unsupported"}:
        return None, candidate.get("next_action") or "preview evidence requires review"
    entry = candidate.get("suggested_entry")
    if not isinstance(entry, dict):
        return None, "missing suggested_entry; run probe.py --batch first"
    name = candidate["name"]
    command = [sys.executable, os.path.abspath(__file__), name]
    url = candidate.get("careers_url")
    if url:
        command.extend(["--url", url])
        if entry.get("ats") == "workday":
            if entry.get("search"):
                command.extend(["--search", str(entry["search"])])
            if entry.get("max_pages"):
                command.extend(["--max-pages", str(entry["max_pages"])])
    else:
        ats = entry.get("ats")
        command.extend(["--ats", str(ats)])
        if ats in ("greenhouse", "ashby", "lever"):
            if not entry.get("slug"):
                return None, f"{ats} suggestion is missing slug"
            command.extend(["--slug", str(entry["slug"])])
        elif ats == "workday":
            for flag, field in (
                    ("--tenant", "tenant"), ("--site", "site"), ("--wd-host", "wd_host")):
                value = entry.get(field)
                if field == "wd_host":
                    value = value or "wd5"
                if not value:
                    return None, f"workday suggestion is missing {field}"
                command.extend([flag, str(value)])
            if entry.get("search"):
                command.extend(["--search", str(entry["search"])])
            if entry.get("max_pages"):
                command.extend(["--max-pages", str(entry["max_pages"])])
        elif ats == "eightfold":
            for flag, field in (("--base-url", "base_url"), ("--domain", "domain")):
                if not entry.get(field):
                    return None, f"eightfold suggestion is missing {field}"
                command.extend([flag, str(entry[field])])
        elif ats == "smartrecruiters":
            if not entry.get("company_id"):
                return None, "smartrecruiters suggestion is missing company_id"
            command.extend(["--company-id", str(entry["company_id"])])
            if entry.get("country"):
                command.extend(["--country", str(entry["country"])])
            for query in entry.get("queries") or ([entry["query"]] if entry.get("query") else []):
                command.extend(["--query", str(query)])
        elif ats == "workable":
            if not entry.get("account"):
                return None, "workable suggestion is missing account"
            command.extend(["--account", str(entry["account"])])
        else:
            # Generic field forwarding keeps reviewed batch onboarding aligned
            # with every adapter declared by hybrid_hunter, including JSON-LD
            # and enterprise adapters that have no dedicated CLI aliases.
            try:
                import hybrid_hunter
                if ats not in hybrid_hunter.KNOWN_ATS_TYPES:
                    return None, f"batch onboarding does not support explicit ats '{ats}'"
            except ImportError:
                return None, f"batch onboarding does not support explicit ats '{ats}'"
            for key, value in entry.items():
                if key in {"name", "ats"} or value is None:
                    continue
                if isinstance(value, (dict, list)):
                    value = yaml.safe_dump(value, default_flow_style=True).strip()
                command.extend(["--field", f"{key}={value}"])
    if no_seed:
        command.append("--no-seed")
    if allow_empty:
        command.append("--allow-empty")
    category = candidate.get("category", "uncategorized")
    command.extend([
        "--comment",
        f"Batch-verified {time.strftime('%Y-%m-%d', time.gmtime())}; category: {category}.",
    ])
    return command, None


def write_review_report(path, candidates, commands, skipped):
    """Write a portable review artifact without changing the input ledger."""
    payload = {
        "version": 1,
        "source": os.path.basename(path),
        "candidates": candidates,
        "actionable": [
            {"name": name, "command": command} for name, command in commands
        ],
        "skipped": [{"name": name, "reason": reason} for name, reason in skipped],
    }
    extension = os.path.splitext(path)[1].lower()
    if extension == ".json":
        import json
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
    elif extension in (".yaml", ".yml"):
        with open(path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(payload, stream, sort_keys=False, width=1000)
    else:
        raise SystemExit("❌ --review-out must end in .yaml, .yml, or .json")


def run_batch(path, apply=False, no_seed=False, approved_names=None,
              allow_empty=False, auto_approve_verified=False, review_out=None):
    """Review or apply explicitly approved candidates from a probe report."""
    try:
        candidates = probe.load_candidates(path)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"❌ {exc}") from exc
    if auto_approve_verified:
        # Refresh evidence in the same bounded-concurrency probe path used by
        # probe.py.  The input ledger remains untouched until --apply succeeds.
        try:
            refreshed = probe.batch_probe(path)
            candidates = refreshed["candidates"]
        except (OSError, ValueError, KeyError) as exc:
            raise SystemExit(f"❌ batch probing failed: {exc}") from exc
    approved_names = {name.casefold() for name in (approved_names or [])}
    known_names = {candidate["name"].casefold() for candidate in candidates}
    unknown_approvals = approved_names - known_names
    if unknown_approvals:
        raise SystemExit(
            "❌ --approve names not found in batch: " + ", ".join(sorted(unknown_approvals)))
    active_names = set()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as stream:
                config = yaml.safe_load(stream) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise SystemExit(f"❌ Could not read {CONFIG_FILE}: {exc}") from exc
        active_names = {
            entry["name"].casefold()
            for entry in ((config.get("ats_companies") or [])
                          + (config.get("custom_pages") or []))
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
    commands = []
    skipped = []
    for candidate in candidates:
        if (auto_approve_verified
                and candidate.get("probe_status") == "verified_endpoint"
                and isinstance(candidate.get("suggested_entry"), dict)
                and isinstance(candidate.get("live_postings"), int)
                and candidate.get("live_postings", 0) >= 1):
            candidate["approved"] = True
            candidate["auto_approved"] = True
        if candidate["name"].casefold() in approved_names:
            candidate["approved"] = True
        if (candidate.get("approved") is True
                and candidate["name"].casefold() in active_names):
            skipped.append((candidate["name"], "already tracked"))
            continue
        command, reason = batch_command(
            candidate, no_seed=no_seed, allow_empty=allow_empty)
        if command:
            commands.append((candidate["name"], command))
        else:
            skipped.append((candidate["name"], reason))

    print(f"Batch contains {len(candidates)} candidate(s): "
          f"{len(commands)} approved and actionable, {len(skipped)} skipped.")
    for name, reason in skipped:
        print(f"  — {name}: {reason}")
    for name, command in commands:
        printable = " ".join(f'"{part}"' if " " in part else part for part in command)
        print(f"  {'▶' if apply else 'DRY RUN'} {name}: {printable}")

    if review_out:
        command_names = {name.casefold() for name, _ in commands}
        for candidate in candidates:
            if candidate["name"].casefold() in command_names:
                continue
            candidate.setdefault(
                "review_reason",
                candidate.get("reason")
                or ("verified endpoint has no live postings"
                    if candidate.get("probe_status") == "verified_endpoint"
                    else f"probe status: {candidate.get('probe_status', 'not approved')}"))
        write_review_report(review_out, candidates, commands, skipped)
        print(f"Review artifact written to {review_out}")

    if not apply:
        print("No files changed. Add `approved: true` after review, then rerun with --apply.")
        return
    if not commands:
        raise SystemExit("❌ No approved, actionable candidates to apply.")

    failures = []
    succeeded = []
    for name, command in commands:
        print(f"\n{'=' * 60}\nAdding {name}\n{'=' * 60}")
        proc = subprocess.run(command, text=True)
        if proc.returncode:
            failures.append(name)
            print(f"❌ {name} failed; continuing so other independently reviewed entries can run.")
        else:
            succeeded.append(name.casefold())
    if succeeded and os.path.splitext(path)[1].lower() in (".yaml", ".yml"):
        by_name = {candidate["name"].casefold(): candidate for candidate in candidates}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for key in succeeded:
            candidate = by_name.get(key)
            if not candidate:
                continue
            candidate["status"] = "active"
            candidate["activated_at"] = now
            if isinstance(candidate.get("suggested_entry"), dict):
                candidate["active_source"] = candidate["suggested_entry"]
        original_ledger = _read_bytes(path)
        rendered = yaml.safe_dump({"version": 1, "candidates": candidates},
                                  sort_keys=False, width=1000)
        _atomic_write(path, rendered, expected=original_ledger)
    if failures:
        raise SystemExit(
            "❌ Batch completed with failures: " + ", ".join(failures)
            + ". Successful entries remain verified and seeded.")
    print(f"\n✅ Added {len(commands)} reviewed candidate(s).")


def sync_active_candidates(path):
    """Update YAML ledger lifecycle status from verified production config."""
    if os.path.splitext(path)[1].lower() not in (".yaml", ".yml"):
        raise SystemExit("❌ --sync-active requires a YAML candidate ledger")
    candidates = probe.load_candidates(path)
    with open(CONFIG_FILE, encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    active = {
        entry["name"].casefold(): entry
        for entry in (config.get("ats_companies") or []) + (config.get("custom_pages") or [])
    }
    changed = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for candidate in candidates:
        entry = active.get(candidate["name"].casefold())
        if not entry:
            continue
        if candidate.get("status") != "active":
            changed += 1
            candidate["status"] = "active"
            candidate["activated_at"] = now
        candidate["active_source"] = {
            key: value for key, value in entry.items()
            if key not in ("name", "keywords")
        }
    with open(path, "w", encoding="utf-8") as stream:
        yaml.safe_dump(
            {"version": 1, "candidates": candidates},
            stream, sort_keys=False, width=1000)
    print(f"✅ Synchronized {changed} candidate status(es) from {CONFIG_FILE}.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", nargs="?",
                        help="Company name (becomes the source name in config.yaml)")
    parser.add_argument("--batch", metavar="PATH",
                        help="Review a YAML/CSV candidate report instead of one company")
    parser.add_argument("--apply", action="store_true",
                        help="With --batch, add only rows explicitly marked approved: true")
    parser.add_argument("--approve", action="append", default=[], metavar="NAME",
                        help="Approve one named batch row for this invocation; repeatable")
    parser.add_argument("--auto-approve-verified", action="store_true",
                        help="With --batch, approve rows whose probe_status is verified_endpoint")
    parser.add_argument("--review-out", metavar="PATH",
                        help="With --batch or --preview, write the review report as YAML or JSON")
    parser.add_argument("--preview", action="store_true",
                        help="Probe and classify one proposed source without changing config/state")
    parser.add_argument("--sync-active", action="store_true",
                        help="With --batch, update YAML statuses from current config and exit")
    parser.add_argument("--url", help="Careers/board URL to probe instead of name-based slug guessing")
    parser.add_argument("--from-job-url", metavar="URL",
                        help="Derive an ATS entry from one concrete job URL")
    parser.add_argument("--ats", help="Skip probing; specify the adapter explicitly")
    parser.add_argument("--field", action="append", default=[], metavar="KEY=VALUE",
                        help="With --ats, set any adapter field verbatim; repeatable")
    parser.add_argument("--slug")
    parser.add_argument("--tenant")
    parser.add_argument("--site")
    parser.add_argument("--wd-host", default="wd5")
    parser.add_argument("--search",
                        help="Workday searchText (defaults to the narrow 'internship')")
    parser.add_argument("--max-pages", type=int,
                        help="Workday page budget (1-12; must cover the verified total)")
    parser.add_argument("--base-url")
    parser.add_argument("--domain")
    parser.add_argument("--company-id")
    parser.add_argument("--country")
    parser.add_argument("--query", action="append")
    parser.add_argument("--account")
    parser.add_argument("--comment",
                        help="Short operational comment written above the config entry")
    parser.add_argument("--no-seed", action="store_true",
                        help="Skip --seed baselining after the entry is verified")
    parser.add_argument("--allow-empty", action="store_true",
                        help="Allow a verified board with zero live postings")
    args = parser.parse_args()

    # URL-only positional convenience for preview (and for future callers): a
    # concrete posting URL is input, never an inferred company name.
    if args.name and probe._looks_like_url(args.name) and not args.url and not args.from_job_url:
        args.from_job_url, args.name = args.name, None

    if args.batch:
        if args.name:
            parser.error("name cannot be combined with --batch")
        if args.preview:
            parser.error("--preview is for one source and cannot be combined with --batch")
        single_only = (
            args.url, args.from_job_url, args.ats, bool(args.field), args.slug, args.tenant, args.site,
            args.wd_host != "wd5", args.search, args.max_pages,
            args.base_url, args.domain, args.company_id, args.country, args.query,
            args.account, args.comment,
        )
        if any(single_only):
            parser.error("single-source ATS flags cannot be combined with --batch")
        if args.sync_active:
            if (args.apply or args.approve or args.auto_approve_verified
                    or args.review_out):
                parser.error(
                    "--sync-active cannot be combined with --apply, --approve, "
                    "--auto-approve-verified, or --review-out")
            sync_active_candidates(args.batch)
            return
        run_batch(
            args.batch, apply=args.apply, no_seed=args.no_seed,
            approved_names=args.approve, allow_empty=args.allow_empty,
            auto_approve_verified=args.auto_approve_verified,
            review_out=args.review_out)
        return
    if args.apply:
        parser.error("--apply requires --batch")
    if args.approve:
        parser.error("--approve requires --batch")
    if args.auto_approve_verified:
        parser.error("--auto-approve-verified requires --batch")
    if args.sync_active:
        parser.error("--sync-active requires --batch")
    if not args.name and not args.from_job_url and not args.url:
        parser.error("name is required unless --batch is used")
    if not args.name and not args.preview:
        parser.error("a company name is required (URL-only input is supported with --preview)")
    if args.field and not args.ats:
        parser.error("--field requires --ats")

    if args.preview:
        if args.no_seed or args.allow_empty or args.comment:
            parser.error("--preview cannot be combined with --no-seed, --allow-empty, or --comment")
        if args.url and args.from_job_url:
            parser.error("--url and --from-job-url are mutually exclusive")
        report = preview_source(args)
        if args.review_out:
            try:
                probe.write_report(report, args.review_out)
            except (OSError, ValueError) as exc:
                raise SystemExit(f"❌ {exc}") from exc
            print(f"Review report written to {args.review_out}")
        else:
            print(yaml.safe_dump(report, sort_keys=False, width=1000).rstrip())
        return

    if args.review_out:
        parser.error("--review-out requires --batch or --preview")

    original_config_bytes = _read_bytes(CONFIG_FILE)
    if original_config_bytes is None:
        raise SystemExit(f"❌ Could not read {CONFIG_FILE}")
    config_text = original_config_bytes.decode("utf-8")
    config = yaml.safe_load(config_text)
    taken = {e["name"].casefold()
             for e in (config.get("ats_companies") or []) + (config.get("custom_pages") or [])}
    if args.name.casefold() in taken:
        raise SystemExit(f"❌ '{args.name}' is already tracked in {CONFIG_FILE}.")

    if args.url and args.from_job_url:
        parser.error("--url and --from-job-url are mutually exclusive")
    entry, count = resolve_entry(args)
    if count == 0 and not args.allow_empty:
        raise SystemExit(
            "❌ Board is structurally valid but has zero live postings; refusing "
            "to add an inactive or possibly stale board. Use --allow-empty only "
            "after independently verifying its identity.")
    shown = "?" if count is None else count
    print(f"✅ {entry['ats']} source verified ({shown} live postings). Adding to {CONFIG_FILE}...")

    comment = args.comment or (
        f"Verified by add_source.py on {time.strftime('%Y-%m-%d', time.gmtime())}.")
    proposed_config_text = insert_entry(config_text, entry, comment=comment)
    state_before = _read_bytes(STATE_FILE)
    try:
        _atomic_write(CONFIG_FILE, proposed_config_text, expected=original_config_bytes)

        print(f"🧪 Verifying: hybrid_hunter.py --test --ats-only --company \"{args.name}\"")
        ok, proc = run_hunter(["--test", "--ats-only"], args.name)
        for line in proc.stdout.splitlines():
            if args.name in line or "match" in line.lower():
                print(f"   {line.strip()}")
        if not ok:
            print(proc.stdout)
            print(proc.stderr, file=sys.stderr)
            raise SystemExit(f"❌ --test run failed — rolled {CONFIG_FILE} back. Entry NOT added.")

        if args.no_seed:
            print(f"⏭️  Skipped seeding. First live run will alert on every open match at {args.name}.")
        else:
            print(f"🌱 Baselining: hybrid_hunter.py --seed --ats-only --company \"{args.name}\"")
            ok, proc = run_hunter(["--seed", "--ats-only"], args.name)
            if not ok:
                print(proc.stdout)
                print(proc.stderr, file=sys.stderr)
                raise SystemExit(
                    f"❌ Seeding failed — rolled {CONFIG_FILE} and state.json back. "
                    "Entry NOT added.")
            for line in proc.stdout.splitlines():
                if line.startswith("🌱"):
                    print(f"   {line.strip()}")
    except BaseException:
        # Roll back all writes, including exceptions and Ctrl-C. Each restore
        # uses the bytes currently on disk as its expected value; if another
        # process edited a file while the probe was running, refuse to clobber
        # that change and leave the recoverable .bak copy for investigation.
        try:
            _restore_bytes(CONFIG_FILE, original_config_bytes, _read_bytes(CONFIG_FILE))
            _restore_bytes(STATE_FILE, state_before, _read_bytes(STATE_FILE))
        except BaseException as rollback_error:
            raise RuntimeError(f"transaction rollback refused: {rollback_error}") from rollback_error
        raise

    print(f"\n✅ {args.name} added and verified. Commit config.yaml"
          + ("" if args.no_seed else " and state.json") + " to make it live.")


if __name__ == "__main__":
    main()
