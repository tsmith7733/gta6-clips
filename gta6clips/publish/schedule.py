"""Daily publish slots: which times the next clips go out at. Pure functions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..config import Config

YOUTUBE_MIN_LEAD = timedelta(minutes=15)   # YouTube refuses a publish_at sooner than this
SEARCH_DAYS = 120


@dataclass(frozen=True)
class SlotSettings:
    tz: ZoneInfo
    weekdays: tuple[time, ...]
    weekends: tuple[time, ...]
    min_lead: timedelta

    @classmethod
    def from_config(cls, cfg: Config) -> "SlotSettings":
        slots = cfg.get("publish.slots", {}) or {}
        lead = timedelta(minutes=float(cfg.get("publish.min_lead_minutes", 20)))
        return cls(tz=ZoneInfo(str(cfg.get("publish.timezone", "America/Chicago"))),
                   weekdays=_times(slots.get("weekdays", ["15:00", "20:00"])),
                   weekends=_times(slots.get("weekends", ["12:00", "15:00", "20:00"])),
                   min_lead=max(lead, YOUTUBE_MIN_LEAD + timedelta(minutes=1)))

    def day_slots(self, day: date) -> tuple[time, ...]:
        return self.weekends if day.weekday() >= 5 else self.weekdays


def _times(values) -> tuple[time, ...]:
    out = []
    for v in values or []:
        hh, mm = str(v).strip().split(":")
        out.append(time(int(hh), int(mm)))
    if not out:
        raise ValueError("publish.slots needs at least one time")
    return tuple(sorted(set(out)))


def _minute(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0)


def next_slots(settings: SlotSettings, *, now: datetime, taken: list[datetime], count: int) -> list[datetime]:
    """The next `count` free slots at least min_lead from now, as aware datetimes in settings.tz.

    `taken` are publish times already used (scheduled uploads); one clip per slot.
    """
    busy = {_minute(t) for t in taken}
    earliest = now + settings.min_lead
    out: list[datetime] = []
    day = now.astimezone(settings.tz).date()
    for _ in range(SEARCH_DAYS):
        for t in settings.day_slots(day):
            slot = datetime.combine(day, t, tzinfo=settings.tz)
            if slot < earliest or _minute(slot) in busy:
                continue
            out.append(slot)
            if len(out) >= count:
                return out
        day += timedelta(days=1)
    return out


def rfc3339(dt: datetime) -> str:
    """With its offset, as YouTube (and Clips Kitty) require: 2026-11-20T15:00:00-06:00."""
    return dt.isoformat(timespec="seconds")


def parse_rfc3339(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"publish time {text!r} has no offset")
    return dt


def label(dt: datetime, tz: ZoneInfo) -> str:
    """'Fri Nov 20, 3:00 PM CST'."""
    local = dt.astimezone(tz)
    return local.strftime("%a %b %d, ").replace(" 0", " ") + local.strftime("%I:%M %p").lstrip("0") + \
        " " + (local.tzname() or "")
