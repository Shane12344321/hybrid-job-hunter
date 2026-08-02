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
import os
import subprocess
import sys
import time

import yaml

import probe

CONFIG_FILE = "config.yaml"
STATE_FILE = "state.json"


def build_explicit_entry(args):
    """Build and live-verify an entry from explicit --ats flags."""
    name, ats = args.name, args.ats
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
        count = probe.check_workday(args.tenant, args.wd_host, args.site)
        if count is None:
            raise SystemExit(f"❌ Workday CXS verify failed (tenant={args.tenant}, "
                             f"wd_host={args.wd_host}, site={args.site}) — not adding.")
        return {"name": name, "ats": "workday", "tenant": args.tenant,
                "wd_host": args.wd_host, "site": args.site}, count
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
    if args.ats:
        return build_explicit_entry(args)
    if args.url:
        entry, info = probe.probe_url(args.url, name=args.name)
        if not entry:
            raise SystemExit(f"❌ {info}")
        return entry, info
    print(f"Probing slug candidates: {', '.join(probe.slug_candidates(args.name))}")
    hits = probe.probe_name(args.name)
    if not hits:
        raise SystemExit(
            "❌ No Greenhouse/Ashby/Lever board found for that name.\n"
            "   Retry with the careers page URL (detects Workday/Eightfold too):\n"
            f"   python3 add_source.py \"{args.name}\" --url https://...\n"
            "   JS-only board or program page? See .agents/skills/add-source.")
    best = max(hits, key=lambda h: h["jobs"])
    if len(hits) > 1 or not probe.slug_is_high_confidence(args.name, best["slug"]):
        for hit in hits:
            print(f"   found {hit['ats']}: '{hit['slug']}' ({hit['jobs']} postings)")
        raise SystemExit(
            "❌ Name-based probing is ambiguous or matched a shortened slug; "
            "refusing to guess the company identity. Review the hits, then use "
            f"`--ats {best['ats']} --slug {best['slug']}` only after confirming "
            "the board belongs to the intended company.")
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


def batch_command(candidate, no_seed=False):
    """Build a single-source command for a reviewed batch candidate."""
    if candidate.get("approved") is not True:
        return None, "not approved"
    entry = candidate.get("suggested_entry")
    if not isinstance(entry, dict):
        return None, "missing suggested_entry; run probe.py --batch first"
    name = candidate["name"]
    command = [sys.executable, os.path.abspath(__file__), name]
    url = candidate.get("careers_url")
    if url:
        command.extend(["--url", url])
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
            return None, f"batch onboarding does not support explicit ats '{ats}'"
    if no_seed:
        command.append("--no-seed")
    category = candidate.get("category", "uncategorized")
    command.extend([
        "--comment",
        f"Batch-verified {time.strftime('%Y-%m-%d', time.gmtime())}; category: {category}.",
    ])
    return command, None


def run_batch(path, apply=False, no_seed=False, approved_names=None):
    """Review or apply explicitly approved candidates from a probe report."""
    try:
        candidates = probe.load_candidates(path)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"❌ {exc}") from exc
    approved_names = {name.casefold() for name in (approved_names or [])}
    known_names = {candidate["name"].casefold() for candidate in candidates}
    unknown_approvals = approved_names - known_names
    if unknown_approvals:
        raise SystemExit(
            "❌ --approve names not found in batch: " + ", ".join(sorted(unknown_approvals)))
    commands = []
    skipped = []
    for candidate in candidates:
        if candidate["name"].casefold() in approved_names:
            candidate = {**candidate, "approved": True}
        command, reason = batch_command(candidate, no_seed=no_seed)
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

    if not apply:
        print("No files changed. Add `approved: true` after review, then rerun with --apply.")
        return
    if not commands:
        raise SystemExit("❌ No approved, actionable candidates to apply.")

    failures = []
    for name, command in commands:
        print(f"\n{'=' * 60}\nAdding {name}\n{'=' * 60}")
        proc = subprocess.run(command, text=True)
        if proc.returncode:
            failures.append(name)
            print(f"❌ {name} failed; continuing so other independently reviewed entries can run.")
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
    parser.add_argument("--sync-active", action="store_true",
                        help="With --batch, update YAML statuses from current config and exit")
    parser.add_argument("--url", help="Careers/board URL to probe instead of name-based slug guessing")
    parser.add_argument("--ats", help="Skip probing; specify the adapter explicitly")
    parser.add_argument("--slug")
    parser.add_argument("--tenant")
    parser.add_argument("--site")
    parser.add_argument("--wd-host", default="wd5")
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

    if args.batch:
        if args.name:
            parser.error("name cannot be combined with --batch")
        single_only = (
            args.url, args.ats, args.slug, args.tenant, args.site,
            args.base_url, args.domain, args.company_id, args.country, args.query,
            args.account, args.comment, args.allow_empty,
        )
        if any(single_only):
            parser.error("single-source ATS flags cannot be combined with --batch")
        if args.sync_active:
            if args.apply or args.approve:
                parser.error("--sync-active cannot be combined with --apply or --approve")
            sync_active_candidates(args.batch)
            return
        run_batch(
            args.batch, apply=args.apply, no_seed=args.no_seed,
            approved_names=args.approve)
        return
    if args.apply:
        parser.error("--apply requires --batch")
    if args.approve:
        parser.error("--approve requires --batch")
    if args.sync_active:
        parser.error("--sync-active requires --batch")
    if not args.name:
        parser.error("name is required unless --batch is used")

    with open(CONFIG_FILE) as f:
        config_text = f.read()
    config = yaml.safe_load(config_text)
    taken = {e["name"].casefold()
             for e in (config.get("ats_companies") or []) + (config.get("custom_pages") or [])}
    if args.name.casefold() in taken:
        raise SystemExit(f"❌ '{args.name}' is already tracked in {CONFIG_FILE}.")

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
    with open(CONFIG_FILE, "w") as f:
        f.write(insert_entry(config_text, entry, comment=comment))

    print(f"🧪 Verifying: hybrid_hunter.py --test --ats-only --company \"{args.name}\"")
    ok, proc = run_hunter(["--test", "--ats-only"], args.name)
    for line in proc.stdout.splitlines():
        if args.name in line or "match" in line.lower():
            print(f"   {line.strip()}")
    if not ok:
        with open(CONFIG_FILE, "w") as f:
            f.write(config_text)
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"❌ --test run failed — rolled {CONFIG_FILE} back. Entry NOT added.")

    if args.no_seed:
        print(f"⏭️  Skipped seeding. First live run will alert on every open match at {args.name}.")
    else:
        print(f"🌱 Baselining: hybrid_hunter.py --seed --ats-only --company \"{args.name}\"")
        state_existed = os.path.exists(STATE_FILE)
        state_before = None
        if state_existed:
            with open(STATE_FILE, "rb") as stream:
                state_before = stream.read()
        ok, proc = run_hunter(["--seed", "--ats-only"], args.name)
        if not ok:
            with open(CONFIG_FILE, "w") as f:
                f.write(config_text)
            if state_existed:
                with open(STATE_FILE, "wb") as stream:
                    stream.write(state_before)
            elif os.path.exists(STATE_FILE):
                os.unlink(STATE_FILE)
            print(proc.stdout)
            print(proc.stderr, file=sys.stderr)
            raise SystemExit(
                f"❌ Seeding failed — rolled {CONFIG_FILE} and state.json back. "
                "Entry NOT added.")
        for line in proc.stdout.splitlines():
            if line.startswith("🌱"):
                print(f"   {line.strip()}")

    print(f"\n✅ {args.name} added and verified. Commit config.yaml"
          + ("" if args.no_seed else " and state.json") + " to make it live.")


if __name__ == "__main__":
    main()
