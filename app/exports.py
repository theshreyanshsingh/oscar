"""Excel workbooks of calls: Call history's "Download as Excel" and Analytics' report.

A workbook has a sheet of calls (one row each: when, who, how long, how it ended,
the summary and what the owner asked to have noted), and a sheet of what was said
on them (one row a transcript line, with its time in the call). The Analytics
report starts with a sheet of its figures and one of calls day by day.

Times are the owner's (the dashboard says its time zone), written as real Excel
dates. Everything a caller said is written as text: a line that begins with "="
stays words, never a formula.
"""

import io
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.analytics import OUTCOMES, outcome

# Characters Excel does not allow in a cell (control characters, bar tab and new lines).
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
KIND_NAMES = {"browser": "Browser", "phone_inbound": "Incoming", "phone_outbound": "Outgoing"}
OUTCOME_NAMES = dict(OUTCOMES) | {"on_now": "Going on now"}
SPEAKERS = {"agent": None, "user": "Caller", "person": "Person put through to", "note": "Note"}
# A row of a sheet is at most this many characters a cell (Excel's own limit is 32,767).
CELL_MOST = 32_000
HEADING_FILL = PatternFill("solid", fgColor="EEF2FE")
HEADING_FONT = Font(bold=True, color="00122E")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")


def _text(value: object) -> str:
    return _ILLEGAL.sub("", "" if value is None else str(value))[:CELL_MOST]


def _local(stamp: str, zone) -> datetime | None:
    """A stored UTC time as the owner's wall-clock time, without a zone (as Excel keeps dates)."""
    try:
        return datetime.fromisoformat(stamp).astimezone(zone).replace(tzinfo=None) if stamp else None
    except (TypeError, ValueError):
        return None


class _Sheet:
    """One sheet written row by row (openpyxl's write-only mode, so a long history fits)."""

    def __init__(self, book: Workbook, title: str, headings: list[str], widths: list[int], wrapped: set[int] = frozenset()):
        self.sheet = book.create_sheet(sheet_title(title))
        self.wrapped = wrapped
        for index, width in enumerate(widths, start=1):
            self.sheet.column_dimensions[get_column_letter(index)].width = width
        self.sheet.freeze_panes = "A2"
        self.rows = 0
        self.heading(headings)
        self.columns = len(headings)

    def add(self, values: list, formats: dict[int, str] | None = None) -> None:
        cells = []
        for index, value in enumerate(values):
            if isinstance(value, str):
                value = _text(value)
            cell = WriteOnlyCell(self.sheet, value=value)
            if isinstance(value, str):
                cell.data_type = "s"  # words stay words, even "=...": never a formula
            if formats and index in formats:
                cell.number_format = formats[index]
            cell.alignment = WRAP if index in self.wrapped else TOP
            cells.append(cell)
        self.sheet.append(cells)
        self.rows += 1

    def heading(self, values: list) -> None:
        """A row that names what follows it, styled as the sheet's own headings."""
        cells = []
        for value in values:
            # A heading can come from someone else's sheet: words, never a formula.
            text = isinstance(value, str)
            cell = WriteOnlyCell(self.sheet, value=_text(value) if text else value)
            if text:
                cell.data_type = "s"
            cell.font = HEADING_FONT
            cell.fill = HEADING_FILL
            cells.append(cell)
        self.sheet.append(cells)
        self.rows += 1

    def gap(self) -> None:
        self.sheet.append([None])
        self.rows += 1

    def filter(self) -> None:
        if self.rows > 1:
            self.sheet.auto_filter.ref = f"A1:{get_column_letter(self.columns)}{self.rows}"


def sheet_title(name: str) -> str:
    """A name Excel takes for a sheet: no [ ] : * ? / \\ or control characters, no ' at
    either end, at most 31 characters, and not "History" (which Excel keeps for itself)."""
    title = _ILLEGAL.sub("", re.sub(r"[\[\]:*?/\\]+", " ", name or ""))
    title = re.sub(r"\s+", " ", title).strip().strip("'").strip()[:31].strip()
    if not title:
        return "Sheet"
    return f"{title[:23]} (sheet)" if title.casefold() == "history" else title


def _number(call: dict) -> str:
    return {"phone_inbound": call.get("from_number"), "phone_outbound": call.get("to_number")}.get(call["kind"]) or ""


def _detail_names(calls: list[dict], asked: list[str]) -> list[str]:
    names = list(asked)
    for call in calls:
        names += [name for name in call.get("details") or {} if name not in names]
    return names


def add_calls(book: Workbook, calls: list[dict], lines: dict[str, list[dict]], zone, asked: list[str]) -> None:
    """The sheet of calls. How each ended goes by its transcript (whether the caller spoke)."""
    names = _detail_names(calls, asked)
    headings = [
        "Started", "Type", "Number", "Caller's name", "Length (min:s)", "How it ended", "Status",
        "Cost ($)", "Summary", "Call back on", "To do", "To do done", "Put through to",
        "Recording (min:s)", "Problem", *names, "Call id",
    ]
    widths = [17, 10, 17, 18, 10, 26, 9, 9, 60, 17, 40, 17, 17, 12, 30, *[22] * len(names), 34]
    sheet = _Sheet(book, "Calls", headings, widths, wrapped={8, 10, 14})
    for call in calls:
        details = call.get("details") or {}
        counted = {**call, "caller_lines": sum(1 for line in lines.get(call["id"], []) if line["role"] == "user")}
        length = call.get("duration_seconds")
        recorded = call.get("recording_seconds") or 0
        sheet.add(
            [
                _local(call["started_at"], zone),
                KIND_NAMES.get(call["kind"], call["kind"]),
                _number(call),
                call.get("caller_name") or "",
                length / 86400 if isinstance(length, int) else None,
                OUTCOME_NAMES.get(outcome(counted), ""),
                call["status"],
                round(float(call.get("cost") or 0), 4),
                call.get("summary") or "",
                call.get("callback_number") or "",
                call.get("follow_up") or "",
                _local(call.get("follow_up_done_at") or "", zone),
                call.get("transferred_to") or "",
                recorded / 86400 if recorded else None,
                call.get("error") or "",
                *[_text(details.get(name, "")) for name in names],
                call["id"],
            ],
            formats={0: "yyyy-mm-dd hh:mm", 4: "[m]:ss", 7: "0.000", 11: "yyyy-mm-dd hh:mm", 13: "[m]:ss"},
        )
    sheet.filter()


def add_transcripts(book: Workbook, calls: list[dict], lines: dict[str, list[dict]], zone, agent_name: str) -> None:
    """The sheet of what was said: a row a line, calls in the order of the Calls sheet."""
    sheet = _Sheet(
        book,
        "Transcripts",
        ["Call started", "Number", "At (min:s)", "Who", "What was said", "Call id"],
        [17, 17, 10, 22, 90, 34],
        wrapped={4},
    )
    for call in calls:
        started = _local(call["started_at"], zone)
        for line in lines.get(call["id"], []):
            at = line.get("at_seconds")
            who = SPEAKERS.get(line["role"], "Caller") or agent_name
            if line["role"] == "user" and call["kind"] == "phone_outbound":
                who = "Person called"
            if line["role"] == "user" and call["kind"] == "browser":
                who = "You"
            sheet.add(
                [started, _number(call), at / 86400 if isinstance(at, (int, float)) else None, who, line["text"], call["id"]],
                formats={0: "yyyy-mm-dd hh:mm", 2: "[m]:ss"},
            )
    sheet.filter()


def add_overview(book: Workbook, report: dict, period: str) -> None:
    """The Analytics figures: the headline ones, how calls ended, what Oscar did, the people
    on most calls and the summary fields, then a sheet of calls day by day."""
    sheet = _Sheet(book, "Overview", ["Analytics", period], [34, 22, 14])
    totals = report["totals"]
    previous = report["previous"]

    def row(label: str, value, before=None, number_format: str | None = None) -> None:
        values = [label, value] + ([before] if before is not None else [])
        formats = {1: number_format, 2: number_format} if number_format else None
        sheet.add(values, formats=formats)

    sheet.heading(["", "This period", "The period before"])
    row("Calls", totals["calls"], previous["calls"])
    row("Conversations (the other side spoke)", totals["conversations"], previous["conversations"])
    row("Share of calls that were conversations", totals["conversation_rate"], previous["conversation_rate"], "0%")
    seconds = totals["average_seconds"]
    before_seconds = previous["average_seconds"]
    row(
        "Average conversation (min:s)",
        seconds / 86400 if seconds is not None else None,
        before_seconds / 86400 if before_seconds is not None else "",
        "[m]:ss",
    )
    row("Talking in all (h:mm)", totals["talk_seconds"] / 86400, previous["talk_seconds"] / 86400, "[h]:mm")
    row("Incoming calls", totals["incoming"], previous["incoming"])
    row("Missed calls", totals["missed"], previous["missed"])
    row("To-dos", totals["todos"], previous["todos"])
    row("To-dos still open", totals["todos_open"])
    row("Cost ($)", totals["cost"], previous["cost"], "0.000")
    sheet.gap()
    sheet.heading(["How calls ended", "Calls"])
    for one in report["outcomes"]:
        sheet.add([one["label"], one["count"]])
    sheet.gap()
    sheet.heading(["How long conversations lasted", "Conversations"])
    for one in report["lengths"]:
        sheet.add([one["label"], one["count"]])
    sheet.gap()
    actions = report["actions"]
    sheet.heading(["What Oscar did on calls", "Times"])
    for label, key in (
        ("Web searches", "web_searches"),
        ("Spreadsheet look-ups", "sheet_lookups"),
        ("Put through to a person", "put_through"),
        ("... who answered", "put_through_answered"),
        ("Voicemails left", "voicemails_left"),
        ("Asked not to be called again", "do_not_call"),
        ("Summaries written", "summaries"),
        ("Calls recorded", "recorded"),
        ("Times Oscar was talked over", "interrupted"),
    ):
        sheet.add([label, actions[key]])
    people = report["people"]
    sheet.gap()
    sheet.heading(["Who was on the calls", "Numbers"])
    sheet.add(["Phone numbers on calls", people["numbers"]])
    sheet.add(["... new", people["new"]])
    sheet.add(["... on more than one call", people["repeat"]])
    for field in report["fields"]:
        sheet.gap()
        sheet.heading([field["name"], "Calls"])
        for one in field["values"]:
            sheet.add([one["value"], one["count"]])
        if field["other"]:
            sheet.add(["Other answers", field["other"]])

    days = _Sheet(
        book,
        "Day by day" if report["over_time"]["bucket"] == "day" else "Week by week",
        ["Day" if report["over_time"]["bucket"] == "day" else "Week of", "Incoming", "Outgoing", "Browser", "Conversations", "Cost ($)"],
        [14, 10, 10, 10, 14, 10],
    )
    for point in report["over_time"]["points"]:
        days.add(
            [datetime.fromisoformat(point["date"]), point["incoming"], point["outgoing"], point["browser"], point["conversations"], point["cost"]],
            formats={0: "yyyy-mm-dd", 5: "0.000"},
        )


def calls_workbook(
    calls: list[dict],
    lines: dict[str, list[dict]],
    *,
    zone,
    agent_name: str,
    asked: list[str],
    report: dict | None = None,
    period: str = "",
) -> bytes:
    book = Workbook(write_only=True)
    if report is not None:
        add_overview(book, report, period)
    add_calls(book, calls, lines, zone, asked)
    add_transcripts(book, calls, lines, zone, agent_name)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def table_workbook(table: dict) -> bytes:
    """A spreadsheet Oscar answers from, as it reads it: its headings, then its rows."""
    book = Workbook(write_only=True)
    title = sheet_title(table["name"])
    sheet = _Sheet(book, title, table["headings"], [min(40, max(12, len(heading) + 4)) for heading in table["headings"]])
    for row in table["rows"]:
        sheet.add(list(row))
    sheet.filter()
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
