"""Tender source adapters.

Only sources whose canonical URLs live under ``source.allowed_hosts`` may be
registered here. Adding a third-party aggregator would violate the project's
single-source rule — see docs/COMPLIANCE.md. Google Search
(:class:`~gem_intel.sources.google_search.GoogleSearchGemSource`) does not
violate this: it only ever *discovers* candidate URLs, which are then
verified against the same allow-list and fetched from the official GeM page
itself — Google is never the origin of a tender fact.
"""

from gem_intel.sources.base import CompositeSource, SourceAdapter, SourceResult

__all__ = ["CompositeSource", "SourceAdapter", "SourceResult"]


def build_source(settings, client, mode: str = "portal"):
    """Construct the source adapter(s) for a run.

    ``mode``:

    * ``"portal"`` (default) — GeM's own search box only. Unchanged
      behaviour from before Google Search discovery existed.
    * ``"google_search"`` — Google Custom Search discovery only. Useful for
      testing the Google Search path in isolation, or as a fallback if the
      portal's own search form breaks.
    * ``"both"`` — runs both and merges/deduplicates results. This is the
      recommended setting once Google Search credentials are configured: it
      adds a second, independent way to find the same official pages
      without ever changing what counts as a verified source.
    """
    from gem_intel.sources.gem_bidplus import GemBidPlusSource
    from gem_intel.sources.google_search import GoogleSearchGemSource

    if mode == "portal":
        return GemBidPlusSource(settings, client)
    if mode == "google_search":
        return GoogleSearchGemSource(settings, client)
    if mode == "both":
        return CompositeSource([
            GemBidPlusSource(settings, client),
            GoogleSearchGemSource(settings, client),
        ])
    raise ValueError(f"Unknown discovery mode: {mode!r} (expected portal/google_search/both)")
