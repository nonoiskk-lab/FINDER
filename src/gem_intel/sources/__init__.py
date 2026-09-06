"""Tender source adapters.

Only sources whose canonical URLs live under ``source.allowed_hosts`` may be
registered here. Adding a third-party aggregator would violate the project's
single-source rule — see docs/COMPLIANCE.md.
"""

from gem_intel.sources.base import SourceAdapter, SourceResult

__all__ = ["SourceAdapter", "SourceResult"]
