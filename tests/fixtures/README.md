# Offline fixtures

HTML captured from the public GeM bid list, used by `pytest` and by
`gem-intel run --dry-run` so the whole pipeline can be exercised without
touching the portal.

Dates use relative tokens — `{{+4d}}`, `{{+4d 15:00}}`, `{{-3d}}` — which
`FixtureSource` expands to real dates at load time. Without that, every
fixture tender would silently become "expired" a week after it was captured
and a dry run would look broken.

The bid numbers and buyer names here are **synthetic**. They exist to exercise
parsing and filtering; they are not real tenders. Re-capture from
`bidplus.gem.gov.in` when the portal's markup changes, and update
`config/selectors.yaml` in the same commit.
