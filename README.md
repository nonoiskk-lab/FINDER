# GeM IT Tender Intelligence Automation

A daily, production-ready pipeline that finds, verifies, analyses, scores,
and reports **IT product and IT service tenders** on the official
[Government e-Marketplace (GeM)](https://gem.gov.in) portal, focused on
opportunities relevant to **Jharkhand** and closing within **10 days**.

```
FIND → VERIFY → FILTER → ANALYZE → SCORE → SUMMARIZE → SAVE
```

Every day it produces a Markdown report (always) and, when configured, a
Google Doc filed into `Drive/GeM Tender Intelligence/<Year>/<Month>/` plus a
Google Sheet mirror of the tender database — see `docs/GOOGLE_SETUP.md`.

## What it guarantees

- **Official GeM source only.** No tender aggregators. Every tender's URL is
  checked against an allow-list twice, independently — see
  `docs/COMPLIANCE.md`.
- **Never guesses.** EMD, performance security, and required documents are
  either confirmed from the portal/documents (with a quoted source) or shown
  as "⚠ Verification Required" — never assumed.
- **Never submits a bid, pays an EMD, or signs anything.** Discovery and
  analysis only. Final decisions stay with a human.
- **Never bypasses access controls.** A CAPTCHA or bot-challenge aborts the
  crawl for that run and is reported as a `GEM ACCESS ISSUE`, not solved.
- **An empty report is explained, not silent.** If the portal couldn't be
  reached or a page's markup changed, the report says exactly that — it never
  looks identical to "there really were no tenders today".

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Tell it about your business (categories you can supply, OEM letters, etc.)
$EDITOR config/company_profile.yaml

# 2. Check credentials and config
python -m gem_intel.cli doctor

# 3. Try it end-to-end with no network access, using bundled fixtures
python -m gem_intel.cli run --dry-run --print

# 4. Run for real
export ANTHROPIC_API_KEY=sk-ant-...
python -m gem_intel.cli run
```

`gem-intel run` (or `python -m gem_intel.cli run`) is the whole daily job.
Wire it to a scheduler — `.github/workflows/daily-report.yml` runs it every
morning at 08:45 IST via GitHub Actions; any cron works just as well.

## CLI

| Command | Purpose |
|---|---|
| `gem-intel run` | The daily pipeline. `--dry-run` replays `tests/fixtures/` with no network. `--no-google`, `--no-llm`, `--query` for overrides. |
| `gem-intel doctor` | Checks config, company profile, AI credentials, Google Workspace access, and (with `--check-portal`) live connectivity to GeM. |
| `gem-intel keywords` | Prints every search term the run will use, grouped by category. |
| `gem-intel db --open` | Lists tenders currently marked open in the local database. |

## Project layout

```
config/            settings.yaml, taxonomy.yaml, selectors.yaml, company_profile.yaml
src/gem_intel/
  http_client.py   compliant, rate-limited access to the GeM portal
  sources/         the GeM adapter (+ an offline fixture replay adapter)
  extract/         HTML parsing, PDF/DOCX/XLSX text extraction
  analyze/         classification, geography, deadlines, EMD, security,
                   requirements, AI analysis, eligibility, scoring
  validate/        the six-point verification gate
  store/           SQLite database, Google auth, Sheets mirror
  report/          the report document model + Markdown/Google Docs renderers
  pipeline.py       orchestrates the whole run
  cli.py            command-line entry points
tests/              one file per concern, plus an offline end-to-end test
docs/               architecture, compliance rules, Google setup
```

See `docs/ARCHITECTURE.md` for the full data-flow diagram and design
principles, and `docs/COMPLIANCE.md` before touching anything in
`http_client.py` or the source allow-list.

## Testing

```bash
pip install -r requirements-dev.txt
pytest                    # 200+ tests, no network required
ruff check src tests
```

`tests/test_pipeline.py` runs the entire pipeline over bundled fixtures
(`tests/fixtures/`) with Google and AI analysis both disabled, so CI never
needs live credentials.
