# Compliance & safety design

This document exists because the automation touches a government portal and
handles money-adjacent figures (EMD, performance security). Read it before
changing anything in `src/gem_intel/http_client.py`, `sources/`, or the
verification gate.

## Single-source rule

The system is only allowed to read tenders from official GeM hosts, enforced
in three independent places so one bug can't defeat all of them:

1. **`GemHttpClient.assert_allowed_host`** — every outbound request is
   checked against `source.allowed_hosts` in `config/settings.yaml` before it
   is made. A request to any other host raises `DisallowedHost` immediately.
2. **`VerificationGate.is_official_source`** — before a tender enters the
   daily report, its `source_url` is re-checked against the same allow-list.
   This is deliberately redundant with (1): a tender that reached the
   pipeline through any future code path (a new adapter, a bug, a bad
   fixture) is still screened here.
3. **The report itself** names its own source rule ("Official GeM References")
   and every tender line includes the URL that was checked, so a human can
   verify the claim in one click.

Adding a new source (a different GeM subdomain, a future authenticated API)
means adding its host to `source.allowed_hosts` *and* explaining in a PR
description why it is an official GeM endpoint. Never add a tender
aggregator, however reputable, to that list.

**Google Search discovery is discovery, not a source.**
`GoogleSearchGemSource` (`sources/google_search.py`) uses Google's Custom
Search API to find pages already indexed under `site:bidplus.gem.gov.in`. It
does not weaken the single-source rule: `GoogleSearchGemSource._tender_from_result`
calls `GemHttpClient.assert_allowed_host` on every result *before* it becomes
a candidate, so a well-ranked result on a non-official host is dropped
outright — not included with a caveat. And no fact ever comes from the
search result's title or snippet: those only seed a placeholder used by the
first-pass screen, and every fact that reaches the report still comes from
fetching and parsing the official GeM page itself, via the same
`sources/detail_fetch.py` helper the direct-search adapter uses. If you add
another discovery mechanism in the future (a different search engine, an RSS
feed, whatever), hold it to this same standard: discover URLs, never facts,
and verify every URL against the allow-list before it is used for anything.

## No circumvention of access controls

`GemHttpClient` treats a CAPTCHA or bot-interstitial as a hard stop:
`AccessBlocked` is raised, the crawl for that source aborts, and the failure
is recorded as a `GEM ACCESS ISSUE` in the report. The code contains no
CAPTCHA-solving, headless-browser fingerprint spoofing, or any technique
whose purpose is to look like something other than an automated client.
`robots.txt` is honoured, and fails **closed** (assumes disallowed) if it
cannot be read — see `RobotsPolicy` in `http_client.py`.

## No financial or legally-binding action

Nothing in this codebase can submit a bid, pay an EMD, upload a document to
GeM, or sign anything. The `docs.google.com` / Drive / Sheets writes are the
only "write" actions the system performs, and they only ever create a report
or a tracking row — never anything on the GeM portal itself. This is by
design, not by omission: there is no bid-submission adapter to disable.

## No fabrication

Every numeric or requirement claim in the report traces back to either:

* a structured field on the GeM portal (`extract/gem_html.py`), or
* a verbatim quote from a downloaded tender document (`analyze/emd.py`,
  `analyze/security.py`, `analyze/requirements.py`), or
* an AI-generated claim carrying its own verbatim quote as evidence
  (`analyze/llm.py`), which is discarded if the quote is missing.

Anywhere the source is silent, the corresponding field renders as
"⚠ Verification Required" rather than a guess. See `models.py`'s docstring
for the underlying data model (`Confidence`, `TernaryFlag`, `Claim`).

## Rate limiting and politeness

`RateLimiter` enforces a floor delay between requests (default 2–5s,
configurable in `source.min_delay_seconds` / `max_delay_seconds`) independent
of `robots.txt`'s `Crawl-delay`, and takes whichever is larger. Retries use
exponential backoff and respect `Retry-After`. The daily run caps itself at
`run.max_listing_pages`, `run.max_detail_fetches`, and
`run.max_document_downloads` so a bug can never turn into a sustained load
test against a government website.
