"""companion_live.py - optional live progress panel for the companion server (roadmap P3-14).

While FileMaker exports a solution (POST /progress) and fm-saxml converts it (POST /convert),
the companion can draw one panel at the bottom of its terminal: the step (export, then convert
and its stage), the file N of M, the time of each finished file, the total elapsed and an
estimate of the time left. A stage that counts units (pages, files) also shows "done of units"
and estimates from the unit rate. Log lines print above it.

Rich is optional. It is imported only when the panel is drawn, so this module and the companion
import and run on the standard library alone. The panel is drawn only when stdout is a terminal,
Rich imports and nobody opted out (``--no-live`` or ``COMPANION_NO_LIVE=1``); otherwise the
companion prints its plain log lines, with no control codes.

The panel shows only while an export or a conversion is in progress, and is empty otherwise: the
companion runs for hours, the finished run's result is already in the log ("Export done",
"convert ... succeeded"), and an idle panel would only take rows from the log.

State comes from callables passed in (``LiveView(get_export_state, get_convert_jobs)``), so this
module never imports companion_server.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

NO_LIVE_ENV = "COMPANION_NO_LIVE"
REFRESH_PER_SECOND = 4
# A run whose last export event is older than this is taken as abandoned (FileMaker closed or
# crashed mid-export) and leaves the panel. One Save a Copy as XML can take minutes on a big file.
EXPORT_STALE_SECONDS = 30 * 60
FINISHED_FILES_SHOWN = 4
LOG_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"     # as the companion's plain log lines
DEFAULT_STAGE_LABELS = {
    "read": "Reading",
    "scrub": "Scrubbing",
    "analyze": "Analyzing",
    "write_raw": "Writing raw XML",
    "write_pages": "Writing pages",
    "write_viewer": "Writing HTML viewer",
    "check": "Checking redactions",
}


def choose_mode(is_tty: bool, rich_available: bool, disabled: bool) -> str:
    """"live" when the panel can be drawn, else "plain"."""
    return "live" if is_tty and rich_available and not disabled else "plain"


def rich_available() -> bool:
    try:
        import rich.live  # noqa: F401
        import rich.logging  # noqa: F401
    except ImportError:
        return False
    return True


def live_disabled(no_live_flag: bool = False, environ=None) -> bool:
    """True with --no-live, or with COMPANION_NO_LIVE set to anything but empty/0/false/no."""
    environ = os.environ if environ is None else environ
    value = environ.get(NO_LIVE_ENV, "").strip().lower()
    return no_live_flag or value not in ("", "0", "false", "no")


def is_terminal(stream, environ=None) -> bool:
    """True when stream is a terminal that understands control codes (TERM=dumb does not)."""
    environ = os.environ if environ is None else environ
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):     # no isatty, or a closed stream
        return False
    return bool(tty) and environ.get("TERM", "") != "dumb"


def format_seconds(seconds) -> str:
    """8.4s, 1m 05s, 1h 02m."""
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _estimate_left(finished_seconds: list, total: int) -> "float | None":
    """Average seconds per finished file × files not finished yet; None before the first file."""
    times = [s for s in finished_seconds if isinstance(s, (int, float)) and not isinstance(s, bool)]
    if not times or not total:
        return None
    remaining = max(0, total - len(finished_seconds))
    return sum(times) / len(times) * remaining


def _estimate_units_left(done, units, stage_elapsed) -> "float | None":
    """Seconds per unit so far × units not done yet; None before the first unit."""
    if not (_is_number(done) and _is_number(stage_elapsed)) or done <= 0:
        return None
    return stage_elapsed / done * max(0, units - done)


def _utc_elapsed(started_at: str, utc_now: datetime) -> "float | None":
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0.0, (utc_now - started).total_seconds())


def build_rows(export_states, convert_jobs, now: "float | None" = None,
               utc_now: "datetime | None" = None, stage_labels: "dict | None" = None) -> list:
    """The panel's rows: one per export or conversion in progress, exports first.

    export_states: the companion's export progress, {casefolded solution: state} or a list of
    states (now is time.monotonic(), the clock their started_at/updated_at use).
    convert_jobs: conversion job dicts with their "progress" block (utc_now is for started_at).
    """
    now = time.monotonic() if now is None else now
    utc_now = datetime.now(timezone.utc) if utc_now is None else utc_now
    labels = DEFAULT_STAGE_LABELS if stage_labels is None else stage_labels
    states = export_states.values() if isinstance(export_states, dict) else (export_states or [])
    rows = []
    for state in sorted(states, key=lambda s: s.get("started_at") or 0):
        if state.get("done") or now - (state.get("updated_at") or now) > EXPORT_STALE_SECONDS:
            continue
        files = state.get("files") or []
        total = state.get("total") or 0
        rows.append({
            "step": "export", "solution": state.get("solution_name", ""), "stage": "",
            "state": "running", "index": state.get("index") or 0, "total": total,
            "file": state.get("file") or "",
            "finished": [(f.get("file"), f.get("seconds")) for f in files],
            "elapsed": now - state["started_at"] if state.get("started_at") is not None else None,
            "left": _estimate_left([f.get("seconds") for f in files], total),
        })
    for job in convert_jobs or []:
        if job.get("done"):
            continue
        progress = job.get("progress") or {}
        stage = progress.get("stage") or ""
        if progress.get("files") and not stage and job.get("state") == "running":
            stage = "finishing"     # after fm-saxml's last event, while the companion reads its output
        files = [f for f in progress.get("files") or [] if f.get("stage") == stage]
        total = progress.get("total") or 0
        elapsed = _utc_elapsed(job.get("started_at") or "", utc_now)
        row = {
            "step": "convert", "solution": job.get("solution_name", ""),
            "stage": labels.get(stage, stage.replace("_", " ").capitalize()) if stage else "",
            "state": job.get("state") or "queued", "index": progress.get("index") or 0, "total": total,
            "file": progress.get("file") or "",
            "finished": [(f.get("file"), f.get("seconds")) for f in files],
            "elapsed": elapsed if elapsed is not None else progress.get("elapsed"),
            "left": _estimate_left([f.get("seconds") for f in files], total),
        }
        units = progress.get("units")
        if _is_number(units) and units > 0 and _is_number(progress.get("done")):
            # A stage with units (write_pages, write_viewer, check): count and estimate by unit
            row.update(units=int(units), done_units=int(progress["done"]), unit=progress.get("unit") or "",
                       left=_estimate_units_left(progress["done"], units, progress.get("stage_elapsed")))
        rows.append(row)
    return rows


def _position_text(row: dict) -> str:
    if row["state"] == "queued":
        return "queued"
    parts = [f"{row['stage']}:"] if row["stage"] else []
    if row.get("units"):
        parts.append(f"{row['done_units']:,} of {row['units']:,} {row['unit']}".rstrip() + ",")
    if row["total"] and row["index"]:
        parts.append(f"file {row['index']} of {row['total']}")
    elif row["total"]:
        parts.append(f"{row['total']} file(s)")
    elif not row["stage"]:
        parts.append("starting")
    if row["file"]:
        parts.append(str(row["file"]))
    return " ".join(parts).rstrip(",")


def _timing_text(row: dict) -> str:
    elapsed = f"{format_seconds(row['elapsed'])} elapsed" if row["elapsed"] is not None else ""
    if row["state"] == "queued" or not row["total"]:     # nothing to count yet, or finishing
        left = ""
    elif row["left"] is None:
        left = "time left: estimating"
    else:
        left = f"about {format_seconds(row['left'])} left" + (" in this stage" if row["step"] == "convert" else "")
    return " · ".join(part for part in (elapsed, left) if part)


def render_panel(rows: list):
    """A Rich renderable for the rows (needs Rich); an empty Text when nothing is in progress."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text

    if not rows:
        return Text("")
    # Per row: "<step>  <solution>  <stage: file N of M name>", then a bar with the times,
    # then the last finished files. Long text is cut with an ellipsis, never wrapped.
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True, min_width=7)                   # step
    table.add_column(no_wrap=True, max_width=24, overflow="ellipsis")   # solution
    table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")  # details
    for row in rows:
        done = len(row["finished"])
        if row.get("units"):
            bar = ProgressBar(total=row["units"], completed=min(row["done_units"], row["units"]), width=20)
        else:
            bar = ProgressBar(total=row["total"] or 1, completed=min(done, row["total"] or 0), width=20)
        timing = Table.grid(padding=(0, 2))
        timing.add_row(bar, Text(_timing_text(row), no_wrap=True, overflow="ellipsis"))
        lines = [Text(_position_text(row), no_wrap=True, overflow="ellipsis"), timing]
        if row["finished"]:
            recent = row["finished"][-FINISHED_FILES_SHOWN:]
            more = f"{done - len(recent)} more, " if done > len(recent) else ""
            text = "finished: " + more + ", ".join(f"{name} {format_seconds(sec)}" for name, sec in recent)
            lines.append(Text(text, style="dim", no_wrap=True, overflow="ellipsis"))
        table.add_row(Text(row["step"], style="bold cyan"), Text(row["solution"], style="bold"), Group(*lines))
    return Panel(table, title="agentic-fm progress", title_align="left", border_style="blue")


class LiveView:
    """The live panel and the Rich log handler that prints log lines above it.

    get_export_state() returns the export states, get_convert_jobs() the conversion jobs;
    both are called a few times a second from Rich's refresh thread and must return copies.
    """

    def __init__(self, get_export_state, get_convert_jobs, *, stage_labels: "dict | None" = None,
                 console=None, refresh_per_second: float = REFRESH_PER_SECOND):
        self._get_export_state = get_export_state
        self._get_convert_jobs = get_convert_jobs
        self._stage_labels = stage_labels
        self._console = console
        self._refresh = refresh_per_second
        self._live = None
        self._saved_handlers = None

    def renderable(self):
        try:
            rows = build_rows(self._get_export_state(), self._get_convert_jobs(),
                              stage_labels=self._stage_labels)
            return render_panel(rows)
        except Exception as exc:     # a bad state must not stop the refresh thread
            from rich.text import Text
            return Text(f"progress panel error: {exc}", style="red")

    def start(self) -> None:
        from rich.console import Console
        from rich.live import Live
        from rich.logging import RichHandler

        if self._console is None:
            self._console = Console(file=sys.stdout)
        handler = RichHandler(console=self._console, show_path=False, markup=False,
                              omit_repeated_times=False, log_time_format=LOG_TIME_FORMAT)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        self._saved_handlers = root.handlers[:]
        root.handlers = [handler]
        self._live = Live(console=self._console, get_renderable=self.renderable, transient=True,
                          refresh_per_second=self._refresh)
        self._live.start()

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        if self._saved_handlers is not None:
            logging.getLogger().handlers = self._saved_handlers
            self._saved_handlers = None


def start_live(get_export_state, get_convert_jobs, *, no_live: bool = False, stream=None,
               stage_labels: "dict | None" = None) -> tuple:
    """Start the panel when choose_mode allows it. Returns (LiveView or None, reason for the log)."""
    stream = sys.stdout if stream is None else stream
    tty, has_rich, disabled = is_terminal(stream), rich_available(), live_disabled(no_live)
    if choose_mode(tty, has_rich, disabled) == "plain":
        reason = ("turned off (--no-live or COMPANION_NO_LIVE)" if disabled
                  else "stdout is not a terminal" if not tty else "Rich is not installed")
        return None, f"Live progress panel: off, {reason}; plain log lines only."
    view = LiveView(get_export_state, get_convert_jobs, stage_labels=stage_labels)
    view.start()
    return view, "Live progress panel: on (--no-live or COMPANION_NO_LIVE=1 turns it off)."
