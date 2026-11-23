"""Excel workbooks for call lists.

A sheet is turned into CSV text, so it goes through the same reading as a CSV file
(app.campaigns.parse_contacts): the same headings, {{column}} fill-ins, problems and
limits. Blank rows are kept as blank lines, so "Row 7" in a problem is row 7 in Excel.

Only .xlsx and .xlsm are read (openpyxl). An old .xls, a .xlsb, a Numbers or an
OpenDocument file is refused with how to save it as one that can be.
"""

import csv
import datetime as dt
import io
import zipfile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.campaigns import CONTACTS_MOST, CSV_MOST_CHARACTERS, FIELD_CHARS

# An .xlsx is zipped XML: 10 MB of it is far more than 5,000 people.
SHEET_MOST_BYTES = 10_000_000
EXCEL_SUFFIXES = (".xlsx", ".xlsm")
OTHER_SPREADSHEETS = (".xls", ".xlsb", ".ods", ".numbers")
COLUMNS_MOST = 50
# Rows with something in them; the people on a list are checked against CONTACTS_MOST later.
ROWS_MOST = CONTACTS_MOST * 2
# A sheet formatted to its last row has a million empty ones: reading stops after this many.
BLANK_ROWS_MOST = 10_000

SAVE_AS = "In Excel, choose File → Save As → Excel Workbook (.xlsx), or CSV."


class SheetError(ValueError):
    """A workbook that cannot be read into a call list, said in words."""


def other_spreadsheet(filename: str) -> str:
    """Why a spreadsheet file Oscar cannot read is refused, or "" for one it can try."""
    name = (filename or "").lower()
    if name.endswith(OTHER_SPREADSHEETS):
        kind = name.rsplit(".", 1)[-1]
        return f"Oscar reads Excel workbooks saved as .xlsx, not .{kind}. {SAVE_AS}"
    return ""


def cell_text(value: object) -> str:
    """A cell as Excel shows it plainly: whole numbers without ".0" (phone numbers are
    often kept as numbers), dates as 2026-11-24, times as 14:30."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return f"{value:.15g}"  # the 15 digits Excel itself keeps
    if isinstance(value, dt.datetime):
        if value.time() == dt.time():
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    return " ".join(str(value).split())


def _rows(sheet) -> tuple[list[list[str]], int]:
    """The sheet's rows as text (blank ones kept, trailing ones not) and how many have something."""
    rows: list[list[str]] = []
    blank_run = 0
    filled = 0
    for values in sheet.iter_rows(max_col=COLUMNS_MOST, values_only=True):
        row = [cell_text(value)[:FIELD_CHARS] for value in values]
        while row and not row[-1]:
            row.pop()
        if not row:
            blank_run += 1
            if blank_run > BLANK_ROWS_MOST:
                break
            continue
        rows.extend([] for _ in range(blank_run))
        blank_run = 0
        rows.append(row)
        filled += 1
        if filled > ROWS_MOST:
            raise SheetError(
                f"That sheet has more than {ROWS_MOST:,} rows. A list can call {CONTACTS_MOST:,} "
                "people at most: split it into smaller lists."
            )
    return rows, filled


def read_sheet(data: bytes, sheet: str = "") -> dict:
    """{"csv", "sheets", "sheet", "rows"}: the rows of `sheet` (by default the first sheet
    that has any) as CSV text, the workbook's visible sheets, the one read, and how many
    rows have something in them."""
    if len(data) > SHEET_MOST_BYTES:
        raise SheetError("That workbook is over 10 MB. Keep just the sheet of people to call, or split it.")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise SheetError(f"That file is not an Excel workbook (.xlsx). {SAVE_AS}")
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError, ValueError, TypeError) as exc:
        raise SheetError(f"That workbook could not be read ({type(exc).__name__}). {SAVE_AS}") from None
    try:
        sheets = [ws for ws in book.worksheets if ws.sheet_state == "visible"]
        if sheet:
            chosen = next((ws for ws in book.worksheets if ws.title == sheet), None)
            if chosen is None:
                raise SheetError(f'That workbook has no sheet called "{sheet}".')
            rows, filled = _rows(chosen)
        else:
            chosen, rows, filled = None, [], 0
            for ws in sheets:
                rows, filled = _rows(ws)
                if filled:
                    chosen = ws
                    break
            if chosen is None:
                raise SheetError("That workbook has no rows in it.")
    finally:
        book.close()
    if not filled:
        raise SheetError(f'The sheet "{chosen.title}" has no rows in it.')

    # Every line as wide as the widest, blank ones too: parse_contacts tells the
    # separator from how evenly it comes on each line, so a comma has to every time.
    width = max(len(row) for row in rows)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow(row + [""] * (width - len(row)))
    text = out.getvalue()
    if len(text) > CSV_MOST_CHARACTERS:
        raise SheetError("That sheet has too much text for one list. Remove the columns Oscar does not need, or split it.")
    return {
        "csv": text,
        "sheets": [ws.title for ws in sheets],
        "sheet": chosen.title,
        "rows": filled,
    }
