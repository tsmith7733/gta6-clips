"""Pure logic of the exact-moments plugin (no SDK), so it can be tested on its own."""

from __future__ import annotations


def parse_ranges(text: str) -> list[tuple[float, float]]:
    """'42.5-101;150-210' -> [(42.5, 101.0), (150.0, 210.0)]. Bad parts raise ValueError."""
    out = []
    for part in (text or "").replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        start, sep, end = part.partition("-")
        if not sep:
            raise ValueError(f"not a start-end range: {part!r}")
        s, e = float(start), float(end)
        if s < 0 or e <= s:
            raise ValueError(f"the range {part!r} ends before it starts")
        out.append((s, e))
    return out


def snap(start: float, end: float, segments: list[dict], limit: float) -> tuple[float, float]:
    """Moves start back (or a little forward) to where a sentence begins and end forward (or a little
    back) to where one ends, each by at most `limit` seconds. Without a break that close, it stays."""
    if limit <= 0 or not segments:
        return start, end
    starts = [float(s["start"]) for s in segments if "start" in s]
    ends = [float(s["end"]) for s in segments if "end" in s]

    def best(t: float, points: list[float], prefer_before: bool) -> float:
        near = [p for p in points if abs(p - t) <= limit]
        if not near:
            return t
        # A start moves earlier (more setup) and an end later (the full reaction) whenever there's a break
        # that way within the limit; the other way only when there isn't.
        right_way = [p for p in near if (p <= t) == prefer_before]
        return min(right_way or near, key=lambda p: abs(p - t))

    new_start = max(0.0, best(start, starts, prefer_before=True))
    new_end = best(end, ends, prefer_before=False)
    if new_end <= new_start + 1:
        return start, end
    return new_start, new_end


def parse_notes(text: str, limit: int = 5, length: int = 160) -> list[str]:
    """'a || b' -> ['a', 'b']: what happens in the moment, for the AI that writes the title.
    At most `limit` notes (Clips Kitty's maximum), each one line of at most `length` characters."""
    notes = []
    for part in (text or "").split("||"):
        note = " ".join(part.split())[:length].rstrip()
        if note and note not in notes:
            notes.append(note)
    return notes[:limit]
