"""Deadline arithmetic and urgency banding.

Days remaining is measured from *now* in the configured timezone to the bid
closing instant, then reported as whole calendar days remaining — the number a
person means when they say "three days left". A bid closing at 15:00 today is
0 days remaining and urgent; one closing at 09:00 tomorrow is 1 day.

An unknown closing time yields ``None``, never a default. Unknown deadlines
cannot pass the filter, because a tender we cannot date is a tender we cannot
promise is still open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from gem_intel.config import Settings
from gem_intel.models import Tender, Urgency


@dataclass
class UrgencyBand:
    label: str
    emoji: str
    min_days: int
    max_days: int

    def contains(self, days: int) -> bool:
        return self.min_days <= days <= self.max_days


class DeadlineAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.tz = ZoneInfo(settings.timezone)
        self.max_days = settings.max_days_remaining
        self.watchlist_max = int(settings.get("deadline.watchlist_max_days_remaining", 60))
        self.bands = [
            UrgencyBand(b["label"], b["emoji"], int(b["min_days"]), int(b["max_days"]))
            for b in settings.urgency_bands
        ]

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # ------------------------------------------------------------------
    def days_remaining(self, closing: datetime | None,
                       now: datetime | None = None) -> int | None:
        if closing is None:
            return None
        reference = now or self.now()
        if closing.tzinfo is None:
            closing = closing.replace(tzinfo=self.tz)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=self.tz)
        delta = closing.astimezone(self.tz).date() - reference.astimezone(self.tz).date()
        days = delta.days
        # A deadline earlier today has already passed by the clock even though
        # the date difference is 0.
        if days == 0 and closing.astimezone(self.tz) < reference.astimezone(self.tz):
            return -1
        return days

    def urgency_for(self, days: int | None) -> Urgency:
        if days is None:
            return Urgency.UNKNOWN
        if days < 0:
            return Urgency.EXPIRED
        for band in self.bands:
            if band.contains(days):
                return Urgency(band.label)
        return Urgency.WATCHLIST

    def band_emoji(self, urgency: Urgency) -> str:
        for band in self.bands:
            if band.label == urgency.value:
                return band.emoji
        return {"WATCHLIST": "🔭", "EXPIRED": "⛔", "UNKNOWN": "⚪"}.get(urgency.value, "")

    # ------------------------------------------------------------------
    def apply(self, tender: Tender, now: datetime | None = None) -> Tender:
        tender.days_remaining = self.days_remaining(tender.bid_end_at, now)
        tender.urgency = self.urgency_for(tender.days_remaining)
        if tender.days_remaining is None:
            tender.flag(
                "Bid closing date could not be read from the official listing — "
                "confirm the deadline on GeM before planning any submission."
            )
        return tender

    def in_main_window(self, tender: Tender) -> bool:
        days = tender.days_remaining
        return days is not None and 0 <= days <= self.max_days

    def in_watchlist_window(self, tender: Tender) -> bool:
        days = tender.days_remaining
        return days is not None and self.max_days < days <= self.watchlist_max
