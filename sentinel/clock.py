"""Time helpers with an injectable "now".

Everything time-related on the farm is expressed in the farm's local day
(``Europe/Budapest`` by default): "4 deaths today" means the local calendar
day, not a UTC one. Tests and the seed script can pin ``now`` so that
"today" is deterministic.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings

UTC = timezone.utc


class Clock:
    """Wall clock with an optional frozen instant (for seeds and tests)."""

    def __init__(self, tz: ZoneInfo | None = None) -> None:
        self.tz = tz or settings.tz
        self._frozen: datetime | None = None

    # -- control ------------------------------------------------------------
    def freeze(self, at: datetime) -> None:
        """Pin ``now()`` to ``at`` (naive values are taken as farm-local)."""
        if at.tzinfo is None:
            at = at.replace(tzinfo=self.tz)
        self._frozen = at

    def unfreeze(self) -> None:
        self._frozen = None

    # -- queries ------------------------------------------------------------
    def now(self) -> datetime:
        """Current instant, tz-aware, in UTC."""
        if self._frozen is not None:
            return self._frozen.astimezone(UTC)
        return datetime.now(UTC)

    def now_local(self) -> datetime:
        return self.now().astimezone(self.tz)

    def today(self) -> date:
        """The farm-local calendar date of ``now()``."""
        return self.now_local().date()

    def local_day(self, instant: datetime) -> date:
        """Farm-local calendar date of any tz-aware instant."""
        return instant.astimezone(self.tz).date()

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        """[start, end) of a farm-local day as UTC instants."""
        start = datetime.combine(day, time.min, tzinfo=self.tz)
        end = start + timedelta(days=1)
        return start.astimezone(UTC), end.astimezone(UTC)

    def day_key(self, instant: datetime) -> str:
        """``yyyy-MM-dd`` in farm-local time — the app's `last_modified_day`."""
        return self.local_day(instant).isoformat()

    def hours_since(self, instant: datetime | None) -> float | None:
        if instant is None:
            return None
        return (self.now() - instant).total_seconds() / 3600.0


clock = Clock()
