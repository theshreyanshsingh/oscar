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
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from openpyxl import load_workbook

from app.sheets import SHEET_MOST_BYTES, SheetError, cell_text, read_sheet
from app.store import Store, utc_now
from app.tables import TableError, read_table

REFRESH_MINUTES = 15
FETCH_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
REDIRECTS_MOST = 5
_ID = r"[A-Za-z0-9_-]{20,100}"
_SHARED = re.compile(rf"/spreadsheets/d/(?P<id>{_ID})(?:/|$)")
_PUBLISHED = re.compile(rf"/spreadsheets/d/e/(?P<id>{_ID})(?:/|$)")
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
    tab_csv_url: str  # the linked tab alone, as CSV ("" for the first tab, or a published sheet)


def sheet_link(url: str) -> SheetLink:
    """The sheet a link is to. Raises SheetError for anything else."""
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        raise SheetError(NOT_A_LINK) from None
    if parts.scheme != "https" or parts.hostname != "docs.google.com" or parts.port not in (None, 443) or parts.username:
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
        return SheetLink(
            view_url=f"https://docs.google.com/spreadsheets/d/e/{key}/pubhtml",
            export_url=f"https://docs.google.com/spreadsheets/d/e/{key}/pub?output=xlsx",
            tab_csv_url="",
        )
    shared = _SHARED.match(parts.path)
    if not shared:
        raise SheetError(NOT_A_LINK)
    key = shared.group("id")
    tab = f"#gid={gid}" if gid else ""
    return SheetLink(
        view_url=f"https://docs.google.com/spreadsheets/d/{key}/edit{tab}",
        export_url=f"https://docs.google.com/spreadsheets/d/{key}/export?format=xlsx",
        tab_csv_url=f"https://docs.google.com/spreadsheets/d/{key}/export?format=csv&gid={gid}" if gid and gid != "0" else "",
    )


def _google_host(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return (
        parts.scheme == "https"
        and parts.port in (None, 443)
        and not parts.username
        and (host == "docs.google.com" or host.endswith(".googleusercontent.com"))
    )


async def _download(client: httpx.AsyncClient, url: str) -> tuple[bytes, httpx.Headers]:
    """Fetch from Google, following its redirects only to Google's own addresses."""
    for _ in range(REDIRECTS_MOST + 1):
        if not _google_host(url):
            raise SheetError("Google sent the sheet somewhere Oscar does not fetch from.")
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("location", ""))
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


def _first_line(rows) -> list[str]:
    for row in rows:
        values = [cell_text(value).strip() for value in row]
        if any(values):
            while values and not values[-1]:
                values.pop()
            return values
    return []


def tab_for(data: bytes, tab_csv: str) -> str:
    """Which sheet of the workbook the linked tab is: the one whose first row is the tab's."""
    wanted = _first_line(csv.reader(io.StringIO(tab_csv)))
    if not wanted:
        return ""
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        for sheet in book.worksheets:
            if sheet.sheet_state == "visible" and _first_line(sheet.iter_rows(max_row=20, values_only=True)) == wanted:
                return sheet.title
    finally:
        book.close()
    return ""


async def read_link(url: str, sheet: str = "", client: httpx.AsyncClient | None = None) -> dict:
    """A Google Sheet's rows, read as an uploaded workbook's are (app/sheets.read_sheet): with
    "title" (the spreadsheet's name) and "source" (its plain link) as well."""
    link = sheet_link(url)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT, headers={"User-Agent": "Oscar"})
    try:
        try:
            data, headers = await _download(client, link.export_url)
            if not sheet and link.tab_csv_url:
                tab_csv, _ = await _download(client, link.tab_csv_url)
                sheet = await asyncio.to_thread(tab_for, data, tab_csv.decode("utf-8", "replace"))
        except httpx.HTTPError as exc:
            raise SheetError(f"Google could not be reached ({type(exc).__name__}). Try again shortly.") from None
    finally:
        if own_client:
            await client.aclose()
    read = await asyncio.to_thread(read_sheet, data, sheet)
    return {**read, "title": _title(headers), "source": link.view_url}


async def refresh_table(store: Store, table: dict, client: httpx.AsyncClient | None = None) -> dict | None:
    """Read a spreadsheet that came from a Google Sheets link again. Its rows change only
    when the sheet's have; when it cannot be read, its rows stay and the reason is kept
    (shown on its card). Returns it as it now is."""
    try:
        read = await read_link(table["source_url"], table["source_sheet"], client)
        headings, rows = await asyncio.to_thread(read_table, read["csv"])
    except (SheetError, TableError) as exc:
        return store.update_lookup_table(table["id"], refresh_error=str(exc)[:300])
    changes: dict = {"refreshed_at": utc_now(), "refresh_error": ""}
    current = store.get_lookup_table(table["id"])
    if current is None:
        return None
    if headings != current["headings"] or rows != current["rows"]:
        changes.update(headings=headings, rows=rows)
    return store.update_lookup_table(table["id"], **changes)
