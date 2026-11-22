"""Exact moments: one clip per time range named in the job's settings, ends snapped to sentence breaks."""

from clipskitty_sdk import run

from ranges import parse_notes, parse_ranges, snap


def main(job):
    ranges = parse_ranges(str(job.settings.get("ranges") or ""))
    if not ranges:
        raise ValueError("No time ranges were given (the 'ranges' setting is empty)")
    segments = job.transcript.segments() if job.transcript is not None else []
    limit = float(job.settings.get("snap_seconds") or 0)
    label = str(job.settings.get("label") or "moment")
    notes = parse_notes(str(job.settings.get("notes") or ""))
    for i, (start, end) in enumerate(ranges):
        s, e = snap(start, end, segments, limit)
        job.log(f"range {start:.1f}-{end:.1f} s -> {s:.1f}-{e:.1f} s")
        moment = job.add_range(s, e, score=max(50, 90 - 5 * i), label=label,
                               reason="the moment the gta6-clips orchestrator picked")
        # What viewers or chat said about the moment, so the title names what actually happens.
        if notes and job.wants("understand"):
            for note in notes:
                job.understand(moment, note)


run(main)
