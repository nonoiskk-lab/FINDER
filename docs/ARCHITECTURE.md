# Architecture

```
                 ┌─────────────────────────────────────────────┐
   TRIGGER       │  cron (GitHub Actions) · Asia/Kolkata 08:45  │
                 └───────────────────────┬─────────────────────┘
                                          ▼
   FIND          gem_intel.sources.gem_bidplus.GemBidPlusSource
                 · searches bidplus.gem.gov.in with every taxonomy keyword
                 · GemHttpClient enforces host allow-list, robots.txt,
                   rate limiting, and aborts (never bypasses) a CAPTCHA
                                          ▼
   EXTRACT       gem_intel.extract.gem_html  (listing + detail HTML)
                 gem_intel.extract.documents (PDF/DOCX/XLSX -> text)
                 · selectors live in config/selectors.yaml, not code
                 · a markup change raises ParserDrift, not "0 tenders"
                                          ▼
   ANALYZE       gem_intel.analyze.*
                 · classifier   — IT product/service, supply vs. service
                 · geo          — Jharkhand relevance (work location, not HQ)
                 · deadline     — days remaining, urgency band
                 · emd          — EMD amount/status, exemptions
                 · security     — Performance Security / ePBG / deposit
                 · requirements — required documents, eligibility, technical
                 · llm          — Claude reads the full document text for the
                                  plain-English summary, risks, next action,
                                  and evidence-gated corrections
                 · eligibility  — "can we bid" verdict, vocabulary-restricted
                                          ▼
   VALIDATE      gem_intel.validate.rules.VerificationGate
                 · six-point gate: official source, active, IT-related,
                   Jharkhand, valid closing date, within 10 days
                 · a bid that clears the window but scores >= 70 goes to
                   the watchlist instead of being dropped
                                          ▼
   SCORE         gem_intel.analyze.scoring.OpportunityScorer
                 · 7 weighted components, 100 points, every component
                   explains its own number
                                          ▼
   SAVE          gem_intel.store.database.TenderDatabase (SQLite)
                 · identity = bid number (or GeM tender ID, or URL hash)
                 · re-seeing a tender updates it and writes to `changes`
                                          ▼
   REPORT        gem_intel.report.builder -> ReportDocument (renderer-neutral)
                    ├── markdown.py -> data/reports/<date>....md   (always)
                    └── gdocs.py    -> Google Doc in Drive          (if configured)
                 gem_intel.store.sheets -> Tenders/Change Log/Run Log tabs
```

## Design principles that show up everywhere

1. **Absence is a value, not a default.** See `models.py`'s module
   docstring. A `Claim` with `confidence=NOT_FOUND` renders as
   "⚠ Verification Required", never as `False`, `0`, or an empty string that
   could be misread as a real answer.

2. **A stage that cannot do its job degrades and says so.** Every analyzer,
   the HTTP client, and the LLM wrapper have an explicit "unavailable" path
   that flags the affected tender or the whole run (`RunManifest.degrade`)
   rather than silently producing a thinner result that looks the same as a
   complete one.

3. **Config over code for anything that drifts.** Search keywords
   (`config/taxonomy.yaml`), HTML selectors (`config/selectors.yaml`), scoring
   weights and deadline bands (`config/settings.yaml`), and the bidding
   company's own capabilities (`config/company_profile.yaml`) are all data,
   not Python, because GeM's markup and a business's capabilities both change
   far more often than the pipeline logic should.

4. **Every stage is independently testable.** `tests/` mirrors this
   structure one file per concern (`test_geo.py`, `test_emd_and_security.py`,
   `test_verification.py`, ...) plus `test_pipeline.py`, which runs the whole
   thing over offline fixtures with no network and no Google credentials.

## Where to make a change

| You want to...                                   | Edit                                  |
|---------------------------------------------------|----------------------------------------|
| Add/remove an IT category or search keyword        | `config/taxonomy.yaml`                |
| Adjust the deadline window or urgency bands        | `config/settings.yaml` → `deadline`   |
| Change scoring weights                             | `config/settings.yaml` → `scoring`    |
| Fix a broken selector after a GeM redesign         | `config/selectors.yaml`               |
| Update your company's capabilities/OEM letters     | `config/company_profile.yaml`         |
| Change what counts as "required document" language | `analyze/requirements.py`             |
| Change the report's structure or wording           | `report/builder.py`                   |
| Add a new tender source (must stay official-only)  | `sources/` + `source.allowed_hosts`   |
