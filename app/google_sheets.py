"""Google Sheets, read from their link: for call lists and the spreadsheets Oscar answers from.

A sheet shared as "Anyone with the link can view", or published to the web, is
fetched from Google as an Excel workbook and read like one that was uploaded
(app/sheets.py). A link to one tab (…#gid=123) reads that tab.

A spreadsheet Oscar answers from that came from a link is read again every
REFRESH_MINUTES (app/main.py), so Oscar answers from the sheet as it is now; if
Google cannot be reached, Oscar keeps answering from the rows it last read.

Only Google's own addresses are fetched: docs.google.com, and the
googleusercontent.com address Google hands the download to.
"""

import asyncio
import csv
import io
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from loguru import logger
from openpyxl import load_workbook

from app.sheets import SHEET_MOST_BYTES, SheetError, cell_text, read_sheet
from app.store import Store, utc_now
from app.tables import TableError, read_table

REFRESH_MINUTES = 15
FETCH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
REDIRECTS_MOST = 5
_ID = r"[A-Za-z0-9_-]{20,100}"
# A signed-in Google user with several accounts gets links with u/1/ in them (/spreadsheets/u/1/d/…).
_SHARED = re.compile(rf"(?:/u/\d+)?/spreadsheets/(?:u/\d+/)?d/(?P<id>{_ID})(?:/|$)")
_PUBLISHED = re.compile(rf"(?:/u/\d+)?/spreadsheets/(?:u/\d+/)?d/e/(?P<id>{_ID})(?:/|$)")
SHARE_IT = (
    'Google would not hand the sheet over. In Google Sheets, choose Share, set General access to '
    '"Anyone with the link" (Viewer), then paste the link again.'
)
NOT_A_LINK = "That is not a link to a Google Sheet. Copy it from the address bar while the sheet is open."


@dataclass(frozen=True)
class SheetLink:
    """A Google Sheet's link, made plain: where to see it, where to fetch it, and its tab."""

    view_url: str  # what the owner opens (and what Oscar keeps)
    export_url: str  # the whole workbook, as .xlsx
    tab_csv_url: str  # the linked tab alone, as CSV ("" when the link names no tab)


def sheet_link(url: str) -> SheetLink:
    """The sheet a link is to. Raises SheetError for anything else."""
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port  # raises for a port that is not a number, or out of range
    except ValueError:
        raise SheetError(NOT_A_LINK) from None
    if parts.scheme != "https" or parts.hostname != "docs.google.com" or port not in (None, 443) or parts.username:
        raise SheetError(NOT_A_LINK)
    gid = ""
    for piece in (parts.fragment, parts.query):
        found = re.search(r"(?:^|[&?#])gid=(\d{1,12})", "&" + piece)
        if found:
            gid = found.group(1)
            break
    published = _PUBLISHED.match(parts.path)
    if published:
        key = published.group("id")
        base = f"https://docs.google.com/spreadsheets/d/e/{key}"
        return SheetLink(
            view_url=f"{base}/pubhtml" + (f"?gid={gid}&single=true" if gid else ""),
            export_url=f"{base}/pub?output=xlsx",
            tab_csv_url=f"{base}/pub?gid={gid}&single=true&output=csv" if gid else "",
        )
    shared = _SHARED.match(parts.path)
    if not shared:
        raise SheetError(NOT_A_LINK)
    key = shared.group("id")
    tab = f"#gid={gid}" if gid else ""
    return SheetLink(
        view_url=f"https://docs.google.com/spreadsheets/d/{key}/edit{tab}",
        export_url=f"https://docs.google.com/spreadsheets/d/{key}/export?format=xlsx",
        tab_csv_url=f"https://docs.google.com/spreadsheets/d/{key}/export?format=csv&gid={gid}" if gid else "",
    )


def _google_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    return (
        parts.scheme == "https"
        and port in (None, 443)
        and not parts.username
        and (host == "docs.google.com" or host.endswith(".googleusercontent.com"))
    )


def _sign_in_page(url: str) -> bool:
    """Whether Google sent the download to its sign-in page: the sheet is not shared."""
    try:
        return urlsplit(url).hostname == "accounts.google.com"
    except ValueError:
        return False


async def _download(client: httpx.AsyncClient, url: str) -> tuple[bytes, httpx.Headers]:
    """Fetch from Google, following its redirects only to Google's own addresses."""
    for _ in range(REDIRECTS_MOST + 1):
        if not _google_host(url):
            raise SheetError("Google sent the sheet somewhere Oscar does not fetch from.")
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("location", ""))
                if _sign_in_page(url):
                    raise SheetError(SHARE_IT)  # never followed: it is not where the sheet is
                continue
            if response.status_code in (401, 403):
                raise SheetError(SHARE_IT)
            if response.status_code == 404:
                raise SheetError("Google has no sheet at that link. Check it, or copy it again.")
            if response.status_code != 200:
                raise SheetError(f"Google answered {response.status_code} when asked for the sheet. Try again shortly.")
            if "text/html" in response.headers.get("content-type", ""):
                raise SheetError(SHARE_IT)  # a sign-in page, not the sheet
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > SHEET_MOST_BYTES:
                    raise SheetError("That sheet is over 10 MB. Keep just the tab Oscar needs.")
            return bytes(body), response.headers
    raise SheetError("Google sent the sheet round too many times. Try again shortly.")


def _title(headers: httpx.Headers) -> str:
    """The spreadsheet's name, from the file name Google gives the download."""
    disposition = headers.get("content-disposition", "")
    found = re.search(r"filename\*=UTF-8''([^;]+)", disposition) or re.search(r'filename="([^"]+)"', disposition)
    name = unquote(found.group(1)) if found else ""
    return re.sub(r"\.(xlsx|csv)$", "", name, flags=re.IGNORECASE).strip()[:80]


def _lines(rows, most: int = 20) -> list[list[str]]:
    """The first few rows with something in them, as plain text, to tell tabs apart by."""
    found: list[list[str]] = []
    for row in rows:
        values = [cell_text(value).strip() for value in row]
        while values and not values[-1]:
            values.pop()
        if values:
            found.append(values)
            if len(found) >= most:
                break
    return found


def visible_tabs(data: bytes) -> list[str]:
    """The names of a workbook's visible sheets. Raises SheetError when it is not one."""
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise SheetError("Google did not send a workbook. Check the link, or try again shortly.")
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise SheetError(f"Google's copy of the sheet could not be read ({type(exc).__name__}).") from None
    try:
        return [sheet.title for sheet in book.worksheets if sheet.sheet_state == "visible"]
    finally:
        book.close()


def tab_for(data: bytes, tab_csv: str) -> str:
    """Which sheet of the workbook the linked tab is: the one sheet whose first rows are the
    tab's. "" when none is, or more than one (two tabs alike, or dates written differently)."""
    wanted = _lines(csv.reader(io.StringIO(tab_csv)))
    if not wanted:
        return ""
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return ""
    try:
        alike = [
            sheet.title
            for sheet in book.worksheets
            if sheet.sheet_state == "visible" and _lines(sheet.iter_rows(max_row=500, values_only=True)) == wanted
        ]
    except Exception:
        return ""
    finally:
        book.close()
    return alike[0] if len(alike) == 1 else ""


async def read_link(url: str, sheet: str = "", client: httpx.AsyncClient | None = None) -> dict:
    """A Google Sheet's rows, read as an uploaded workbook's are (app/sheets.read_sheet): with
    "title" (the spreadsheet's name), "source" (its plain link) and "linked_tab" (the sheet
    the link points to, when it names one and that can be told; "" otherwise).

    A link to one tab reads that tab. When it cannot be told which sheet of the workbook
    that is, or `sheet` is no longer there (renamed in Google), the tab's own rows are
    read as Google gives them, with "sheet" "". """
    link = sheet_link(url)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT, headers={"User-Agent": "Oscar"})
    tab_csv = None
    linked = ""
    try:
        try:
            data, headers = await _download(client, link.export_url)
            tabs = await asyncio.to_thread(visible_tabs, data)
            if link.tab_csv_url and (not sheet or sheet not in tabs):
                tab_bytes, _ = await _download(client, link.tab_csv_url)
                tab_csv = tab_bytes.decode("utf-8-sig", "replace")
                linked = await asyncio.to_thread(tab_for, data, tab_csv)
                sheet = linked
        except httpx.HTTPError as exc:
            raise SheetError(f"Google could not be reached ({type(exc).__name__}). Try again shortly.") from None
    finally:
        if own_client:
            await client.aclose()
    about = {"title": _title(headers), "source": link.view_url, "linked_tab": linked}
    if tab_csv is not None and not sheet:
        rows = sum(1 for line in csv.reader(io.StringIO(tab_csv)) if any(cell.strip() for cell in line))
        if not rows:
            raise SheetError("The tab in that link has no rows in it.")
        return {"csv": tab_csv, "sheets": tabs, "sheet": "", "rows": rows, **about}
    read = await asyncio.to_thread(read_sheet, data, sheet)
    return {**read, **about}


async def refresh_table(store: Store, table: dict, client: httpx.AsyncClient | None = None) -> dict | None:
    """Read a spreadsheet that came from a Google Sheets link again. Its rows change only
    when the sheet's have; when it cannot be read, its rows stay and the reason is kept
    (shown on its card). If the spreadsheet was changed while it was being read (a file put
    in its place, another tab chosen), what was read is dropped. Returns it as it now is."""
    headings = rows = None
    problem = ""
    try:
        read = await read_link(table["source_url"], table["source_sheet"], client)
        headings, rows = await asyncio.to_thread(read_table, read["csv"])
    except (SheetError, TableError) as exc:
        problem = str(exc)
    except Exception as exc:
        logger.exception(f"Reading the spreadsheet {table['id']} from Google Sheets failed")
        problem = f"It could not be read ({type(exc).__name__})."
    return await asyncio.to_thread(_keep_refresh, store, table, headings, rows, problem)


def _keep_refresh(store: Store, table: dict, headings, rows, problem: str) -> dict | None:
    current = store.get_lookup_table(table["id"])
    if current is None:
        return None
    asked = (table["source_url"], table["source_sheet"], table["updated_at"])
    if (current["source_url"], current["source_sheet"], current["updated_at"]) != asked:
        return current  # changed while it was being read: what was read is out of date
    if problem:
        return store.update_lookup_table(table["id"], refresh_error=problem[:300])
    changes: dict = {"refreshed_at": utc_now(), "refresh_error": ""}
    if headings != current["headings"] or rows != current["rows"]:
        changes.update(headings=headings, rows=rows)
    return store.update_lookup_table(table["id"], **changes)
