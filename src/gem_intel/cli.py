"""Command-line entry points.

    gem-intel run            # the daily job
    gem-intel run --dry-run  # replay saved fixtures, touch nothing external
    gem-intel doctor         # check credentials, config and connectivity
    gem-intel keywords       # print every search term that will be used
    gem-intel db --open      # list tenders currently open in the database
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from gem_intel.config import ConfigError, load_settings
from gem_intel.observability import get_logger, setup_logging

log = get_logger(__name__)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="directory holding settings.yaml (default: ./config)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--json-logs", action="store_true",
                        help="emit machine-readable logs (use in CI)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gem-intel",
        description="GeM IT Tender Intelligence Automation — daily discovery, "
                    "verification, analysis and reporting of official GeM tenders.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the daily pipeline")
    _add_common(run)
    run.add_argument("--date", type=_parse_date, default=None,
                     help="report date (YYYY-MM-DD); defaults to today in IST")
    run.add_argument("--query", action="append", default=None,
                     help="override the search terms (repeatable)")
    run.add_argument("--dry-run", action="store_true",
                     help="replay fixtures from tests/fixtures instead of the portal, "
                          "and skip all Google writes")
    run.add_argument("--fixtures", type=Path, default=Path("tests/fixtures"),
                     help="fixture directory for --dry-run")
    run.add_argument("--no-google", action="store_true",
                     help="skip Google Docs/Drive/Sheets even if credentials exist")
    run.add_argument("--no-llm", action="store_true",
                     help="skip AI analysis; rule-based extraction only")
    run.add_argument("--discovery", choices=["portal", "google_search", "both"],
                     default="portal",
                     help="how to find candidate tenders: GeM's own search box "
                          "(default), Google Custom Search only, or both merged. "
                          "google_search/both need GOOGLE_SEARCH_API_KEY and "
                          "GOOGLE_SEARCH_CSE_ID — see docs/GOOGLE_SETUP.md")
    run.add_argument("--print", dest="print_report", action="store_true",
                     help="print the report to stdout when finished")

    doctor = sub.add_parser("doctor", help="check configuration and credentials")
    _add_common(doctor)
    doctor.add_argument("--check-portal", action="store_true",
                        help="also make one live request to the GeM portal")

    keywords = sub.add_parser("keywords", help="print every configured search term")
    _add_common(keywords)

    db = sub.add_parser("db", help="inspect the tender database")
    _add_common(db)
    db.add_argument("--open", dest="show_open", action="store_true",
                    help="list tenders currently marked open")
    db.add_argument("--limit", type=int, default=40)

    return parser


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


# ----------------------------------------------------------------------
def command_run(args: argparse.Namespace) -> int:
    from gem_intel.pipeline import TenderPipeline

    overrides: dict[str, str] = {}
    if args.no_google or args.dry_run:
        overrides["GEMINTEL_GOOGLE__ENABLED"] = "false"
    if args.no_llm:
        overrides["GEMINTEL_LLM__ENABLED"] = "false"
    if args.dry_run:
        # A dry run must not touch the network at all, so attachment downloads
        # are off too — fixtures have no fetchable documents.
        overrides["GEMINTEL_DOCUMENTS__DOWNLOAD"] = "false"

    import os

    settings = load_settings(args.config_dir, environ={**os.environ, **overrides})

    source = None
    client = None
    if args.dry_run:
        from gem_intel.sources.gem_bidplus import FixtureSource

        if not args.fixtures.exists():
            print(f"Fixture directory not found: {args.fixtures}", file=sys.stderr)
            return 2
        source = FixtureSource(settings, args.fixtures)
        log.info("dry run: replaying fixtures", directory=str(args.fixtures))

    pipeline = TenderPipeline(settings, source=source, client=client,
                              discovery_mode=args.discovery)
    try:
        result = pipeline.run(report_date=args.date, queries=args.query)
    finally:
        pipeline.close()

    _print_outcome(result)
    if args.print_report and result.document_path:
        print()
        print(result.document_path.read_text(encoding="utf-8"))

    # A run that could not reach the portal is a failure for the scheduler,
    # even though it produced a (correctly empty) report.
    return 0 if result.manifest.source_reachable else 1


def _print_outcome(result) -> None:
    manifest = result.manifest
    report = result.report
    print()
    print(f"GeM IT Tender Intelligence — {report.report_date:%d %B %Y}")
    print(f"  run id            : {manifest.run_id}")
    print(f"  bids seen         : {manifest.listings_seen}")
    print(f"  detail pages read : {manifest.details_fetched}")
    print(f"  documents read    : {manifest.documents_downloaded}")
    print(f"  in today's report : {report.total_found} "
          f"({report.strong} strong, {report.good} good, {report.urgent} urgent)")
    print(f"  watchlist         : {len(report.watchlist)}")
    print(f"  new / updated     : {manifest.new_tenders} / {manifest.updated_tenders}")
    if manifest.rejected:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(manifest.rejected.items()))
        print(f"  filtered out      : {detail}")
    if result.document_path:
        print(f"  report file       : {result.document_path}")
    if result.doc_url:
        print(f"  google doc        : {result.doc_url}")
    if manifest.degraded_modes:
        print("  degraded          :")
        for mode in manifest.degraded_modes:
            print(f"      - {mode}")
    if manifest.access_issues:
        print("  ACCESS ISSUES     :")
        for issue in manifest.access_issues:
            print(f"      - {issue.render()}")
        print("      (an empty report above does NOT mean there were no tenders)")
    print()


def command_doctor(args: argparse.Namespace) -> int:
    import os

    settings = load_settings(args.config_dir)
    ok = True

    print("Configuration")
    print(f"  config dir        : {settings.config_dir}")
    print(f"  timezone          : {settings.timezone}")
    print(f"  deadline window   : {settings.max_days_remaining} days")
    print(f"  allowed hosts     : {', '.join(sorted(settings.allowed_hosts))}")
    print(f"  search terms      : {len(settings.search_queries())}")

    from gem_intel.analyze.profile import load_profile

    profile = load_profile(settings.config_dir / "company_profile.yaml")
    print("\nCompany profile")
    if profile.configured:
        print(f"  ✓ configured      : {profile.legal_name or '(name not set)'}")
        print(f"    strong areas    : {', '.join(profile.strong) or 'none'}")
        print(f"    OEM letters     : {', '.join(profile.oem_held) or 'none recorded'}")
    else:
        ok = False
        print("  ✗ not configured  : edit config/company_profile.yaml, otherwise "
              "every eligibility verdict will be 'Requires Verification'")

    print("\nAI analysis")
    from gem_intel.analyze.llm import LlmAnalyzer

    analyzer = LlmAnalyzer(settings)
    if analyzer.budget.available:
        print(f"  ✓ ready           : {analyzer.model} (effort={analyzer.effort})")
    else:
        ok = False
        print(f"  ✗ unavailable     : {analyzer.budget.disabled_reason}")

    print("\nGoogle Workspace")
    if not settings.get("google.enabled", True):
        print("  – disabled in settings")
    else:
        from gem_intel.store.google_auth import try_build_clients

        clients = try_build_clients()
        if clients:
            print(f"  ✓ authenticated   : {clients.principal}")
        else:
            ok = False
            print("  ✗ unavailable     : see docs/GOOGLE_SETUP.md "
                  "(the run will still write a local Markdown report)")

    print("\nGoogle Search discovery (optional, separate from the account above)")
    gs_cfg = settings.get("source.google_search", {}) or {}
    if not gs_cfg.get("enabled", False):
        print("  – disabled in settings (source.google_search.enabled: false)")
        print("    this is normal; the direct portal search is the primary path")
    else:
        api_key = os.environ.get(gs_cfg.get("api_key_env", "GOOGLE_SEARCH_API_KEY"), "")
        cse_id = os.environ.get(gs_cfg.get("cse_id_env", "GOOGLE_SEARCH_CSE_ID"), "")
        if api_key and cse_id:
            print(f"  ✓ configured      : {len(gs_cfg.get('site_filters', []))} site "
                  f"filter(s), budget {gs_cfg.get('daily_query_budget', 90)} "
                  "queries/day")
        else:
            missing = [name for name, value in (
                (gs_cfg.get("api_key_env", "GOOGLE_SEARCH_API_KEY"), api_key),
                (gs_cfg.get("cse_id_env", "GOOGLE_SEARCH_CSE_ID"), cse_id),
            ) if not value]
            ok = False
            print(f"  ✗ enabled but missing: {', '.join(missing)} "
                  "(see docs/GOOGLE_SETUP.md → Google Search discovery)")

    print("\nStorage")
    db_path = Path(str(settings.get("storage.database_url", "sqlite:///data/tenders.db"))
                   .replace("sqlite:///", ""))
    print(f"  database          : {db_path} "
          f"({'exists' if db_path.exists() else 'will be created'})")

    if args.check_portal:
        print("\nGeM portal")
        from gem_intel.http_client import AccessBlocked, GemHttpClient

        client = GemHttpClient(settings)
        url = settings.get("source.base_url", "https://bidplus.gem.gov.in") + \
            settings.get("source.all_bids_path", "/all-bids")
        try:
            response = client.fetch(url, stage="doctor")
        except AccessBlocked:
            response = None
            print("  ✗ a human-verification challenge was presented — the automation "
                  "stops here by design")
        if response is not None and response.ok:
            print(f"  ✓ reachable       : HTTP {response.status_code}, "
                  f"{len(response.text):,} bytes in {response.elapsed_ms} ms")
        else:
            ok = False
            for issue in client.access_issues:
                print(f"  ✗ {issue.error_type}: {issue.detail}")
        client.close()

    print()
    print("All checks passed." if ok else
          "Some checks failed — the run will still work, but in a degraded mode.")
    return 0 if ok else 1


def command_keywords(args: argparse.Namespace) -> int:
    settings = load_settings(args.config_dir)
    for name, group in settings.all_groups.items():
        print(f"\n{group.get('label', name)}  [{group.get('_kind')}]")
        for query in group.get("queries", []) or []:
            print(f"  · {query}")
    print(f"\n{len(settings.search_queries())} unique search terms in total.")
    return 0


def command_db(args: argparse.Namespace) -> int:
    settings = load_settings(args.config_dir)
    from gem_intel.store.database import TenderDatabase

    database = TenderDatabase(
        settings.get("storage.database_url", "sqlite:///data/tenders.db")
    )
    try:
        rows = database.open_tenders()[: args.limit]
        if not rows:
            print("No open tenders in the database.")
            return 0
        print(f"{'Bid Number':<28} {'Closes':<17} {'Score':>5}  Title")
        print("-" * 100)
        for row in rows:
            closes = (row["bid_end_at"] or "")[:16].replace("T", " ")
            print(f"{(row['bid_number'] or '')[:27]:<28} {closes:<17} "
                  f"{row['score'] or 0:>5.0f}  {(row['title'] or '')[:44]}")
        print(f"\n{len(rows)} open tender(s).")
    finally:
        database.close()
    return 0


COMMANDS = {
    "run": command_run,
    "doctor": command_doctor,
    "keywords": command_keywords,
    "db": command_db,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, json_output=args.json_logs)
    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
