"""Excel workbooks for call lists (app/sheets.py): read into the same rows as a CSV."""

import datetime as dt
import io
import zipfile

import pytest
from openpyxl import Workbook

from app import sheets
from app.campaigns import parse_contacts
from app.sheets import SheetError, cell_text, other_spreadsheet, read_sheet


def workbook(*sheets_rows: tuple[str, list[list]], hidden: tuple[str, ...] = ()) -> bytes:
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets_rows:
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
        if title in hidden:
            sheet.sheet_state = "hidden"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


PEOPLE = [
    ["Name", "Phone", "Quote", "Visited"],
    ["Sam Taylor", 61412345678, "Back fence", dt.datetime(2026, 11, 20)],
    ["Lee Chen", "+61 498 765 432", "Gate; side path, both", dt.date(2026, 11, 21)],
    ["Ana Ruiz", 412000111.0, 1250.5, None],  # a mobile kept as a number loses its 0
]


def test_a_sheet_reads_like_the_same_csv():
    read = read_sheet(workbook(("People", PEOPLE)))
    assert (read["sheet"], read["sheets"], read["rows"]) == ("People", ["People"], 4)
    contacts, headings, problems = parse_contacts(read["csv"], "61")
    assert headings == ["Name", "Phone", "Quote", "Visited"]
    assert problems == []
    assert [contact["number"] for contact in contacts] == ["+61412345678", "+61498765432", "+61412000111"]
    assert contacts[0]["fields"] == {"Name": "Sam Taylor", "Quote": "Back fence", "Visited": "2026-11-20"}
    # Commas and semicolons in a cell stay in it.
    assert contacts[1]["fields"]["Quote"] == "Gate; side path, both"
    assert contacts[2]["fields"] == {"Name": "Ana Ruiz", "Quote": "1250.5"}


def test_row_numbers_in_problems_are_the_rows_in_excel():
    rows = [["Name", "Phone"], ["Sam", "+61412345678"], [], [], ["Lee", "not a number"], ["", ""], ["Kim", "+61412345678"]]
    contacts, _, problems = parse_contacts(read_sheet(workbook(("Sheet1", rows)))["csv"])
    assert len(contacts) == 1
    assert problems == ["Row 5: not a number is not a phone number.", "Row 7: +61412345678 is already on the list."]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (61412345678, "61412345678"),
        (61412345678.0, "61412345678"),
        (0.1 + 0.2, "0.3"),
        (True, "TRUE"),
        (dt.datetime(2026, 11, 24, 14, 30), "2026-11-24 14:30"),
        (dt.date(2026, 11, 24), "2026-11-24"),
        (dt.time(9, 5), "09:05"),
        ("  Sam\n Taylor ", "Sam Taylor"),
    ],
)
def test_cells_read_as_excel_shows_them(value, text):
    assert cell_text(value) == text


def test_the_first_sheet_with_rows_is_read_and_another_can_be_chosen():
    data = workbook(
        ("Notes", []),
        ("Hidden", [["Phone"], ["+61400000001"]]),
        ("November", [["Phone"], ["+61400000002"]]),
        ("December", [["Phone"], ["+61400000003"], ["+61400000004"]]),
        hidden=("Hidden",),
    )
    read = read_sheet(data)
    assert read["sheet"] == "November"
    assert read["sheets"] == ["Notes", "November", "December"]  # hidden sheets are not offered
    december = read_sheet(data, "December")
    assert (december["sheet"], december["rows"]) == ("December", 3)
    with pytest.raises(SheetError, match='no sheet called "January"'):
        read_sheet(data, "January")
    with pytest.raises(SheetError, match='"Notes" has no rows'):
        read_sheet(data, "Notes")


def test_an_empty_workbook_says_so():
    with pytest.raises(SheetError, match="no rows"):
        read_sheet(workbook(("Sheet1", [])))


@pytest.mark.parametrize("name", ["people.xls", "People.XLSB", "people.ods", "people.numbers"])
def test_spreadsheets_oscar_cannot_read_say_how_to_save_them(name):
    assert "Save As → Excel Workbook (.xlsx)" in other_spreadsheet(name)


@pytest.mark.parametrize("name", ["people.xlsx", "people.xlsm", "people.csv", ""])
def test_files_it_can_read_are_let_through(name):
    assert other_spreadsheet(name) == ""


def test_a_file_that_is_not_a_workbook_is_refused_in_words():
    with pytest.raises(SheetError, match=r"not an Excel workbook \(\.xlsx\)"):
        read_sheet(b"Name,Phone\nSam,+61412345678\n")
    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("hello.txt", "not a workbook")
    with pytest.raises(SheetError, match="could not be read"):
        read_sheet(broken.getvalue())


def test_a_sheet_formatted_to_its_last_row_stops_reading_at_the_blank_ones(monkeypatch):
    monkeypatch.setattr(sheets, "BLANK_ROWS_MOST", 5)
    rows = [["Phone"], ["+61400000001"]] + [[]] * 6 + [["+61400000002"]]
    read = read_sheet(workbook(("Sheet1", rows)))
    assert read["rows"] == 2 and "+61400000002" not in read["csv"]


def test_too_many_rows_are_refused(monkeypatch):
    monkeypatch.setattr(sheets, "ROWS_MOST", 3)
    with pytest.raises(SheetError, match="more than 3 rows"):
        read_sheet(workbook(("Sheet1", [["Phone"], ["+61400000001"], ["+61400000002"], ["+61400000003"]])))


def test_a_workbook_too_big_is_refused_before_reading(monkeypatch):
    monkeypatch.setattr(sheets, "SHEET_MOST_BYTES", 100)
    with pytest.raises(SheetError, match="over 10 MB"):
        read_sheet(workbook(("Sheet1", PEOPLE)))


def test_the_dashboard_sends_a_workbook_and_gets_its_rows(client):
    data = workbook(("Leads", PEOPLE), ("Old", [["Phone"], ["+61400000009"]]))
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response = client.post("/api/campaigns/sheet", files={"file": ("leads.xlsx", data, xlsx)})
    assert response.status_code == 200, response.text
    read = response.json()
    assert (read["sheet"], read["sheets"], read["rows"]) == ("Leads", ["Leads", "Old"], 4)
    preview = client.post("/api/campaigns/preview", json={"contacts_csv": read["csv"], "country_code": "+61"})
    assert preview.json()["count"] == 3

    other = client.post("/api/campaigns/sheet", files={"file": ("leads.xlsx", data, xlsx)}, data={"sheet": "Old"})
    assert other.json()["sheet"] == "Old"

    refused = client.post("/api/campaigns/sheet", files={"file": ("leads.xls", b"\xd0\xcf\x11\xe0", "application/vnd.ms-excel")})
    assert refused.status_code == 400
    assert "not .xls" in refused.json()["detail"]
