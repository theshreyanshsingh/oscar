"""Google Sheets read from their link (app/google_sheets.py), and a spreadsheet's rows and
download in the dashboard."""

import io

import httpx
import pytest
from openpyxl import Workbook, load_workbook

from app import google_sheets
from app.google_sheets import read_link, refresh_table, sheet_link, tab_for
from app.sheets import SheetError
from app.store import Store

KEY = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abc"
VIEW = f"https://docs.google.com/spreadsheets/d/{KEY}/edit"
EXPORT = f"https://docs.google.com/spreadsheets/d/{KEY}/export?format=xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx(*sheets: tuple[str, list[list]]) -> bytes:
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets:
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


PRICES = ("Prices", [["Product", "Price"], ["Gate", 310], ["Panel", 120]])
STOCK = ("Stock", [["Item", "In stock"], ["Gate", 4]])


def google(answers: dict[str, httpx.Response], asked: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if asked is not None:
            asked.append(str(request.url))
        return answers.get(str(request.url)) or httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def workbook_answer(data: bytes, name: str = "Harbour prices") -> httpx.Response:
    return httpx.Response(
        200,
        content=data,
        headers={"content-type": XLSX, "content-disposition": f"attachment; filename=\"{name}.xlsx\"; filename*=UTF-8''{name.replace(' ', '%20')}.xlsx"},
    )


@pytest.mark.parametrize(
    "link, view, tab",
    [
        (VIEW, VIEW, ""),
        (f"{VIEW}?usp=sharing", VIEW, ""),
        # Tab 0 is the first one made, which is not always the first one now: read it by its id too.
        (f"{VIEW}#gid=0", f"{VIEW}#gid=0", f"https://docs.google.com/spreadsheets/d/{KEY}/export?format=csv&gid=0"),
        # A signed-in user with several Google accounts gets links with /u/1/ in them.
        (f"https://docs.google.com/spreadsheets/u/1/d/{KEY}/edit#gid=5", f"{VIEW}#gid=5",
         f"https://docs.google.com/spreadsheets/d/{KEY}/export?format=csv&gid=5"),
        (f"https://docs.google.com/spreadsheets/d/e/{KEY}/pubhtml?gid=9&single=true",
         f"https://docs.google.com/spreadsheets/d/e/{KEY}/pubhtml?gid=9&single=true",
         f"https://docs.google.com/spreadsheets/d/e/{KEY}/pub?gid=9&single=true&output=csv"),
        (f"{VIEW}#gid=12345", f"{VIEW}#gid=12345", f"https://docs.google.com/spreadsheets/d/{KEY}/export?format=csv&gid=12345"),
        (f"https://docs.google.com/spreadsheets/d/{KEY}/htmlview?gid=77", f"{VIEW}#gid=77",
         f"https://docs.google.com/spreadsheets/d/{KEY}/export?format=csv&gid=77"),
        (f"https://docs.google.com/spreadsheets/d/e/{KEY}/pubhtml", f"https://docs.google.com/spreadsheets/d/e/{KEY}/pubhtml", ""),
    ],
)
def test_a_link_is_made_plain(link, view, tab):
    found = sheet_link(link)
    assert (found.view_url, found.tab_csv_url) == (view, tab)


@pytest.mark.parametrize(
    "link",
    [
        "",
        "not a link",
        f"http://docs.google.com/spreadsheets/d/{KEY}/edit",
        f"https://docs.google.com.evil.test/spreadsheets/d/{KEY}/edit",
        f"https://evil.test/spreadsheets/d/{KEY}/edit",
        f"https://docs.google.com/document/d/{KEY}/edit",
        f"https://someone@docs.google.com/spreadsheets/d/{KEY}/edit",
        f"https://docs.google.com:8443/spreadsheets/d/{KEY}/edit",
        f"https://docs.google.com:99999/spreadsheets/d/{KEY}/edit",  # not a port at all
        f"https://docs.google.com:abc/spreadsheets/d/{KEY}/edit",
    ],
)
def test_only_a_google_sheet_is_taken(link):
    with pytest.raises(SheetError, match="not a link to a Google Sheet"):
        sheet_link(link)


async def test_a_shared_sheet_is_read_like_an_uploaded_workbook():
    asked: list[str] = []
    async with google({EXPORT: workbook_answer(xlsx(PRICES, STOCK))}, asked) as client:
        read = await read_link(f"{VIEW}?usp=sharing", client=client)
    assert asked == [EXPORT]
    assert read["sheets"] == ["Prices", "Stock"] and read["sheet"] == "Prices"
    assert read["csv"].splitlines() == ["Product,Price", "Gate,310", "Panel,120"]
    assert (read["title"], read["source"]) == ("Harbour prices", VIEW)


async def test_the_linked_tab_is_the_one_read():
    tab = f"https://docs.google.com/spreadsheets/d/{KEY}/export?format=csv&gid=999"
    answers = {
        EXPORT: workbook_answer(xlsx(PRICES, STOCK)),
        tab: httpx.Response(200, text="Item,In stock\r\nGate,4\r\n", headers={"content-type": "text/csv"}),
    }
    async with google(answers) as client:
        read = await read_link(f"{VIEW}#gid=999", client=client)
        assert read["sheet"] == "Stock"
        # Another tab chosen by name is read as asked.
        assert (await read_link(f"{VIEW}#gid=999", "Prices", client=client))["sheet"] == "Prices"


def test_a_tab_is_known_by_its_first_row():
    data = xlsx(PRICES, STOCK)
    assert tab_for(data, "Item,In stock\nGate,4\n") == "Stock"
    assert tab_for(data, "Nothing,Like,It\n") == ""


async def test_google_may_hand_the_download_to_its_own_content_address():
    content = "https://doc-0s-1c-sheets.googleusercontent.com/export/abc"
    answers = {
        EXPORT: httpx.Response(307, headers={"location": content}),
        content: workbook_answer(xlsx(PRICES)),
    }
    async with google(answers) as client:
        assert (await read_link(VIEW, client=client))["sheet"] == "Prices"


async def test_a_download_sent_anywhere_else_is_not_followed():
    asked: list[str] = []
    answers = {EXPORT: httpx.Response(302, headers={"location": "https://evil.test/steal"})}
    async with google(answers, asked) as client:
        with pytest.raises(SheetError, match="somewhere Oscar does not fetch from"):
            await read_link(VIEW, client=client)
    assert asked == [EXPORT]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(403),
        httpx.Response(401),
        # Google answers a sheet that is not shared with its sign-in page.
        httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html; charset=utf-8"}),
    ],
)
async def test_a_sheet_not_shared_says_how_to_share_it(answer):
    async with google({EXPORT: answer}) as client:
        with pytest.raises(SheetError, match="Anyone with the link"):
            await read_link(VIEW, client=client)


async def test_a_sheet_too_big_is_refused(monkeypatch):
    monkeypatch.setattr(google_sheets, "SHEET_MOST_BYTES", 100)
    async with google({EXPORT: workbook_answer(xlsx(PRICES))}) as client:
        with pytest.raises(SheetError, match="over 10 MB"):
            await read_link(VIEW, client=client)


async def test_google_out_of_reach_says_so():
    def handler(request):
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SheetError, match="could not be reached"):
            await read_link(VIEW, client=client)


async def test_a_linked_spreadsheet_is_read_again(tmp_path):
    store = Store(tmp_path / "oscar.db")
    table = store.add_lookup_table(
        name="Prices", description="", private=False, file_name="Harbour prices",
        headings=["Product", "Price"], rows=[["Gate", "310"]], source_url=VIEW, source_sheet="Prices",
    )
    assert table["refreshed_at"]
    changed = ("Prices", [["Product", "Price"], ["Gate", 330], ["Post", 25]])
    async with google({EXPORT: workbook_answer(xlsx(changed, STOCK))}) as client:
        table = await refresh_table(store, table, client)
    assert store.get_lookup_table(table["id"])["rows"] == [["Gate", "330"], ["Post", "25"]]
    assert table["row_count"] == 2 and table["refresh_error"] == ""

    # Unchanged: read again, but not changed (nor marked as updated).
    updated = table["updated_at"]
    async with google({EXPORT: workbook_answer(xlsx(changed))}) as client:
        table = await refresh_table(store, table, client)
    assert table["updated_at"] == updated

    # Google out of reach: the rows stay, and why is kept for the card.
    async with google({EXPORT: httpx.Response(403)}) as client:
        table = await refresh_table(store, table, client)
    assert "Anyone with the link" in table["refresh_error"]
    assert store.get_lookup_table(table["id"])["rows"] == [["Gate", "330"], ["Post", "25"]]
    store.close()


# ----- the dashboard's routes -----


def test_a_link_is_read_for_the_dashboard(client, monkeypatch):
    async def fake_read(url, sheet="", client=None):
        if "bad" in url:
            raise SheetError("That is not a link to a Google Sheet.")
        return {"csv": "Product,Price\nGate,310\n", "sheets": ["Prices"], "sheet": "Prices", "rows": 2, "title": "Prices", "source": VIEW}

    monkeypatch.setattr("app.main.read_link", fake_read)
    assert client.post("/api/sheets/link", json={"url": VIEW}).json()["sheet"] == "Prices"
    refused = client.post("/api/sheets/link", json={"url": "https://bad.test"})
    assert refused.status_code == 400 and "not a link" in refused.json()["detail"]


def test_a_spreadsheet_from_a_link_keeps_it_and_a_file_unlinks_it(client):
    added = client.post(
        "/api/tables",
        json={"name": "Prices", "rows_csv": "Product,Price\nGate,310\n", "source_url": f"{VIEW}?usp=sharing", "source_sheet": "Prices"},
    ).json()["table"]
    assert (added["source_url"], added["source_sheet"]) == (VIEW, "Prices")
    assert added["refreshed_at"]
    refused = client.post("/api/tables", json={"name": "X", "rows_csv": "A,B\n1,2\n", "source_url": "https://evil.test/x"})
    assert refused.status_code == 400
    changed = client.put(
        f"/api/tables/{added['id']}", json={"rows_csv": "Product,Price\nGate,320\n", "file_name": "prices.xlsx", "source_url": ""}
    ).json()["table"]
    assert (changed["source_url"], changed["source_sheet"], changed["refreshed_at"]) == ("", "", "")
    not_linked = client.post(f"/api/tables/{added['id']}/refresh")
    assert not_linked.status_code == 400


def test_read_it_again_now(client, monkeypatch):
    added = client.post(
        "/api/tables", json={"name": "Prices", "rows_csv": "Product,Price\nGate,310\n", "source_url": VIEW}
    ).json()["table"]

    async def fake_read(url, sheet="", client=None):
        return {"csv": "Product,Price\nGate,330\nPost,25\n", "sheets": ["Prices"], "sheet": "Prices", "rows": 3, "title": "", "source": VIEW}

    monkeypatch.setattr(google_sheets, "read_link", fake_read)
    table = client.post(f"/api/tables/{added['id']}/refresh").json()["table"]
    assert table["row_count"] == 2 and table["sample"] == [["Gate", "330"], ["Post", "25"]]


def test_a_spreadsheets_rows_page_by_page_and_searched(client):
    rows = "\n".join(f"Item {n},{'blue' if n % 3 == 0 else 'red'}" for n in range(1, 61))
    added = client.post("/api/tables", json={"name": "Stock", "rows_csv": f"Item,Colour\n{rows}\n"}).json()["table"]
    first = client.get(f"/api/tables/{added['id']}/rows", params={"limit": 25}).json()
    assert (len(first["rows"]), first["found"], first["total"]) == (25, 60, 60)
    assert first["rows"][0] == ["Item 1", "red"]
    later = client.get(f"/api/tables/{added['id']}/rows", params={"limit": 25, "offset": 50}).json()
    assert [row[0] for row in later["rows"]] == [f"Item {n}" for n in range(51, 61)]
    # Every word, anywhere in the row, whatever its capitals.
    found = client.get(f"/api/tables/{added['id']}/rows", params={"q": "BLUE item 1"}).json()
    assert [row[0] for row in found["rows"]] == ["Item 12", "Item 15", "Item 18", "Item 21", "Item 51"]
    assert found["found"] == 5
    assert client.get("/api/tables/nothere/rows").status_code == 404


def test_a_spreadsheet_downloads_as_excel(client):
    added = client.post(
        "/api/tables", json={"name": "Prices: 2026/27", "rows_csv": "Product,Price\nGate,=1+1\n"}
    ).json()["table"]
    response = client.get(f"/api/tables/{added['id']}.xlsx")
    assert response.status_code == 200
    assert 'filename="Prices 202627.xlsx"' in response.headers["content-disposition"]
    sheet = load_workbook(io.BytesIO(response.content)).worksheets[0]
    assert sheet.title == "Prices 2026 27"
    assert [list(row) for row in sheet.iter_rows(values_only=True)] == [["Product", "Price"], ["Gate", "=1+1"]]
    assert sheet["B2"].data_type == "s"  # written as words, not a formula


async def test_a_sheet_not_shared_is_sent_to_sign_in_and_says_how_to_share_it():
    asked: list[str] = []
    answers = {EXPORT: httpx.Response(302, headers={"location": "https://accounts.google.com/ServiceLogin?continue=x"})}
    async with google(answers, asked) as client:
        with pytest.raises(SheetError, match="Anyone with the link"):
            await read_link(VIEW, client=client)
    assert asked == [EXPORT]  # the sign-in page itself is never fetched


async def test_two_tabs_alike_read_the_one_in_the_link():
    tab = f"https://docs.google.com/spreadsheets/d/{KEY}/export?format=csv&gid=2026"
    last_year = ("2025", [["Month", "Sales"], ["Jan", 10]])
    this_year = ("2026", [["Month", "Sales"], ["Jan", 10]])  # the same so far: no telling them apart
    answers = {
        EXPORT: workbook_answer(xlsx(last_year, this_year)),
        tab: httpx.Response(200, text="Month,Sales\r\nJan,10\r\nFeb,12\r\n", headers={"content-type": "text/csv"}),
    }
    async with google(answers) as client:
        read = await read_link(f"{VIEW}#gid=2026", client=client)
    # Rows 2 and 3 of the tab tell it from 2025's; its own rows are read, whatever its name.
    assert read["sheet"] == "" and read["linked_tab"] == ""
    assert read["csv"].splitlines() == ["Month,Sales", "Jan,10", "Feb,12"]
    assert read["sheets"] == ["2025", "2026"] and read["rows"] == 3


async def test_a_tab_renamed_in_google_is_still_read_by_its_link():
    tab = f"https://docs.google.com/spreadsheets/d/{KEY}/export?format=csv&gid=999"
    answers = {
        EXPORT: workbook_answer(xlsx(PRICES, ("Stock 2026", STOCK[1]))),
        tab: httpx.Response(200, text="Item,In stock\r\nGate,4\r\n", headers={"content-type": "text/csv"}),
    }
    async with google(answers) as client:
        read = await read_link(f"{VIEW}#gid=999", "Stock", client=client)  # "Stock" is now "Stock 2026"
    assert read["sheet"] == "Stock 2026" and read["linked_tab"] == "Stock 2026"


async def test_something_that_is_not_a_workbook_says_so():
    answers = {EXPORT: httpx.Response(200, content=b"not a workbook", headers={"content-type": XLSX})}
    async with google(answers) as client:
        with pytest.raises(SheetError, match="did not send a workbook"):
            await read_link(f"{VIEW}#gid=3", client=client)


async def test_what_was_read_is_dropped_when_the_sheet_changed_meanwhile(tmp_path):
    store = Store(tmp_path / "oscar.db")
    table = store.add_lookup_table(
        name="Prices", description="", private=False, file_name="Harbour prices",
        headings=["Product", "Price"], rows=[["Gate", "310"]], source_url=VIEW, source_sheet="Prices",
    )

    def handler(request):
        # The owner puts a file in its place while Google's copy is on its way.
        store.update_lookup_table(table["id"], headings=["Product", "Price"], rows=[["Fence", "99"]],
                                  file_name="prices.xlsx", source_url="", source_sheet="", refreshed_at="")
        return workbook_answer(xlsx(("Prices", [["Product", "Price"], ["Gate", 330]])))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        after = await refresh_table(store, table, client)
    assert after["rows"] == [["Fence", "99"]] and after["source_url"] == "" and after["refreshed_at"] == ""
    store.close()


def test_a_spreadsheets_headings_download_as_words(client):
    added = client.post(
        "/api/tables", json={"name": "History", "rows_csv": '"=HYPERLINK(""http://x"")",Price\nGate,5\n'}
    ).json()["table"]
    response = client.get(f"/api/tables/{added['id']}.xlsx")
    assert response.status_code == 200
    sheet = load_workbook(io.BytesIO(response.content)).worksheets[0]
    assert sheet.title == "History (sheet)"  # Excel keeps "History" for itself
    assert sheet["A1"].value == '=HYPERLINK("http://x")' and sheet["A1"].data_type == "s"
