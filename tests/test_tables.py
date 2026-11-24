"""Spreadsheets Oscar looks things up in (app/tables.py), and how calls and the dashboard use them."""

import json

import httpx
import pytest

from app.search import READER_MODEL, TablesRequest, WebLookup, parse_verdict
from app.tables import (
    Table,
    TableError,
    look_up,
    matching_rows,
    own_rows,
    read_table,
    tables_summary,
    words_of,
)

OPENROUTER = "https://openrouter.test/api/v1"

PRICES = (
    "Price list, November 2026\n"
    "\n"
    "Product,Colour,Price,In stock\n"
    "Garden gate,Black,$420,12\n"
    "Picket fence panel,White,$89,140\n"
    "Pool fence panel,Clear glass,$310,0\n"
    "Chocolate post cap,Brown,$6.50,500\n"
)


def price_list(**fields) -> Table:
    headings, rows = read_table(PRICES)
    return Table(id="p", name="Price list", description="What we sell and what it costs", headings=headings, rows=rows, **fields)


def orders(count: int = 5000, **fields) -> Table:
    rows = [
        [f"ORD-{10000 + i}", f"Customer {i}", f"+61 4{i:08d}", ["Shipped", "Packing", "Delivered"][i % 3], f"{i % 28 + 1} Dec"]
        for i in range(count)
    ]
    return Table(id="o", name="Orders", description="", headings=["Order", "Name", "Phone", "Status", "Delivery"], rows=rows, **fields)


# Reading a sheet


def test_the_headings_are_found_under_a_title():
    headings, rows = read_table(PRICES)
    assert headings == ["Product", "Colour", "Price", "In stock"]
    assert rows[0] == ["Garden gate", "Black", "$420", "12"]
    assert len(rows) == 4


def test_empty_and_repeated_headings_get_names_and_empty_columns_go():
    headings, rows = read_table("Name,,Name,Notes\nSam,x,Taylor,\nLee,y,Chen,\n")
    assert headings == ["Name", "Column 2", "Name 2"]  # "Notes" has nothing in it
    assert rows == [["Sam", "x", "Taylor"], ["Lee", "y", "Chen"]]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "no rows"),
        ("Product,Price\n", "headings but no rows"),
    ],
)
def test_a_sheet_with_nothing_to_look_up_is_refused(text, problem):
    with pytest.raises(TableError, match=problem):
        read_table(text)


def test_a_sheet_too_big_is_refused(monkeypatch):
    from app import tables

    monkeypatch.setattr(tables, "TABLE_ROWS_MOST", 2)
    with pytest.raises(TableError, match="3 rows"):
        read_table("A\n1\n2\n3\n")


# Finding rows


def test_words_are_compared_without_case_accents_or_plural_s():
    assert words_of("Cafés GATES Glass") == ["cafe", "gate", "glass"]


def test_the_rows_that_match_best_come_first():
    table = price_list()
    assert [table.rows[i][0] for i in matching_rows(table, "pool fence")] == ["Pool fence panel", "Picket fence panel"]
    # The start of a word finds the rest of it, and a plural its singular.
    assert [table.rows[i][0] for i in matching_rows(table, "choc caps")] == ["Chocolate post cap"]
    assert [table.rows[i][0] for i in matching_rows(table, "gates")] == ["Garden gate"]
    assert matching_rows(table, "what do you have") == []  # nothing that says which row


def test_a_rare_word_counts_and_one_every_row_has_does_not():
    table = orders()
    # "ORD" is in every row: only the order with that number matches.
    assert [table.rows[i][0] for i in matching_rows(table, "ORD-10234 status")] == ["ORD-10234"]
    assert [table.rows[i][0] for i in matching_rows(table, "order 10234")] == ["ORD-10234"]


def test_a_callers_rows_are_found_by_the_number_they_call_from():
    table = orders(100)
    assert own_rows(table, "+61400000042") == [42]
    assert own_rows(table, "0400 000 042") == [42]  # written the local way
    assert own_rows(table, "+61499999999") == []
    assert own_rows(table, "123") == []  # too short to be anyone's number


def test_a_small_sheet_is_read_whole():
    (found,) = look_up([price_list()], "anything", None)
    assert found.rows == 4
    assert found.text.startswith('The spreadsheet "Price list": all 4 of its rows. It holds: What we sell')
    assert "Chocolate post cap | Brown | $6.50 | 500" in found.text


def test_a_big_sheet_shows_the_rows_that_match():
    (found,) = look_up([orders()], "ORD-10234", None)
    assert found.rows == 1
    assert 'has 5,000 rows. These are the 1 that best match "ORD-10234"' in found.text
    assert "ORD-10234 | Customer 234" in found.text
    (nothing,) = look_up([orders()], "zebra", None)
    assert nothing.rows == 0 and "none matches" in nothing.text


def test_a_private_sheet_tells_a_caller_only_their_own_rows():
    table = orders(private=True)
    (mine,) = look_up([table], "where is my order", "+61 400 000 042")
    assert mine.rows == 1 and "ORD-10042" in mine.text
    assert "Customer 41" not in mine.text and "Customer 43" not in mine.text
    assert mine.cells[0][-1] == "yes" and mine.headings[-1] == "Calling from this number"
    # Another caller, or one whose number is not known, hears nothing of it.
    (theirs,) = look_up([table], "ORD-10042", "+61 499 999 999")
    assert theirs.rows == 0 and "private" in theirs.text and "ORD-10042" not in theirs.text
    (unknown,) = look_up([table], "ORD-10042", "")
    assert unknown.rows == 0
    # The owner, on a browser call, sees every row.
    (owner,) = look_up([table], "ORD-10042", None)
    assert owner.rows == 1 and "ORD-10042" in owner.text


def test_on_a_sheet_that_is_not_private_the_callers_own_rows_are_marked_and_come_first():
    (found,) = look_up([orders()], "my order", "+61400000042")
    assert found.cells[0][0] == "ORD-10042" and found.cells[0][-1] == "yes"


def test_the_decision_is_shown_what_each_sheet_holds():
    summary = tables_summary([price_list(), orders(3)])
    assert '- "Price list" (4 rows): What we sell and what it costs' in summary
    assert "Columns: Product, Colour, Price, In stock" in summary
    assert "e.g. Product: Garden gate; Colour: Black; Price: $420; In stock: 12" in summary
    assert '- "Orders" (3 rows)' in summary


# On a call


def reading(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def tables_lookup(answer: str = "It is three hundred and ten dollars.", *, tables=None, caller=None, **options):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json=reading(answer))

    lookup = WebLookup(
        firecrawl_api_key=options.pop("firecrawl_api_key", ""),
        openrouter_api_key="sk-or-test",
        openrouter_base_url=OPENROUTER,
        agent_name="Oscar",
        agent_instructions="Be {brief}.",
        today="Tuesday 24 November 2026",
        tables=[price_list()] if tables is None else tables,
        caller=caller,
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        **options,
    )
    return lookup, requests


def test_a_sheet_verdict_gives_the_words_and_the_question():
    assert parse_verdict("SHEET\npool fence panel\nHow much is a pool fence panel?") == TablesRequest(
        words="pool fence panel", question="How much is a pool fence panel?"
    )
    assert parse_verdict("**SHEET**\nORD-10234") == TablesRequest(words="ORD-10234", question="ORD-10234")


@pytest.mark.parametrize("web", [False, True])
def test_with_spreadsheets_the_decision_is_told_about_them_and_can_choose_them(web):
    lookup, _ = tables_lookup(firecrawl_api_key="fc-test" if web else "")
    told = lookup._router_instructions
    assert lookup.has_tables
    assert '- "Price list" (4 rows): What we sell and what it costs' in told
    assert "they come before the fact sheet, searching the internet and Oscar answering by itself" in told
    assert "SHEET\n<the words to look for in them" in told
    assert ("SEARCH\n" in told) is web
    assert "Be {brief}." in told and "{name}" not in told and "{tables}" not in told


def test_without_spreadsheets_the_decision_is_not_told_of_any():
    lookup, _ = tables_lookup(tables=[], firecrawl_api_key="fc-test")
    assert not lookup.has_tables and "SHEET" not in lookup._router_instructions


async def test_a_sheet_verdict_without_spreadsheets_is_answered_by_oscar_itself():
    lookup, _ = tables_lookup("SHEET\ngate\nHow much is a gate?", tables=[], firecrawl_api_key="fc-test")
    turn = {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "UklG", "format": "wav"}}]}
    assert await lookup.decide([], turn) is None


async def test_the_answer_is_written_from_the_rows_with_long_numbers_spelt_out_here():
    table = Table(
        id="c", name="Branches", description="", headings=["Branch", "Phone"],
        rows=[["Bondi", "02 5550 0142"], ["Manly", "02 5550 0199"]],
    )
    lookup, requests = tables_lookup("Our Bondi branch is on [number 1].", tables=[table])
    earlier = [{"role": "assistant", "content": "Hi, this is Oscar."}, {"role": "user", "content": "Hi."}]
    answer, found = await lookup.answer_from_tables(
        TablesRequest(words="Bondi phone", question="What's the Bondi number?"), earlier
    )
    assert answer == "Our Bondi branch is on zero two, five five five zero, zero one four two."
    assert [(part.table, part.rows) for part in found] == [("Branches", 2)]
    (asked,) = [json.loads(request.content) for request in requests]
    assert asked["model"] == READER_MODEL and asked["temperature"] == 0
    system, user = asked["messages"]
    assert "using only the rows given" in system["content"] and "Never guess" in system["content"]
    assert "Tuesday 24 November 2026" in system["content"]
    told = user["content"]
    assert "Caller: Hi." in told and "The caller's question now: What's the Bondi number?" in told
    assert "Bondi | [number 1]" in told and "5550" not in told


# The dashboard


def test_the_dashboard_adds_changes_tries_and_deletes_a_spreadsheet(client, monkeypatch):
    preview = client.post("/api/tables/preview", json={"rows_csv": PRICES}).json()
    assert preview["headings"] == ["Product", "Colour", "Price", "In stock"] and preview["count"] == 4

    added = client.post(
        "/api/tables",
        json={"name": "Price list", "description": "What we sell", "rows_csv": PRICES, "file_name": "prices.xlsx"},
    ).json()["table"]
    assert (added["row_count"], added["private"], added["file_name"]) == (4, False, "prices.xlsx")
    assert added["sample"][0] == ["Garden gate", "Black", "$420", "12"]
    assert "rows" not in added
    listed = client.get("/api/tables").json()
    assert [table["name"] for table in listed["tables"]] == ["Price list"]

    changed = client.put(f"/api/tables/{added['id']}", json={"private": True, "rows_csv": "Product,Price\nGate,$5\n"}).json()["table"]
    assert (changed["private"], changed["row_count"], changed["headings"]) == (True, 1, ["Product", "Price"])

    async def answer(self, asked, earlier):
        return f"You asked: {asked.question}", look_up(self._tables, asked.words, self._caller)

    monkeypatch.setattr(WebLookup, "answer_from_tables", answer)
    tried = client.post("/api/tables/ask", json={"question": "How much is a gate?"}).json()
    assert tried["answer"] == "You asked: How much is a gate?"
    # The dashboard is the owner: a private sheet's rows are shown.
    assert tried["found"] == [{"table": "Price list", "rows": 1, "headings": ["Product", "Price"], "cells": [["Gate", "$5"]]}]

    assert client.delete(f"/api/tables/{added['id']}").json() == {"deleted": added["id"]}
    assert client.get("/api/tables").json()["tables"] == []
    assert client.delete(f"/api/tables/{added['id']}").status_code == 404


def test_a_sheet_that_cannot_be_read_is_refused_in_words(client):
    response = client.post("/api/tables", json={"name": "Empty", "rows_csv": "Product,Price\n"})
    assert response.status_code == 400
    assert response.json()["detail"] == "The sheet has headings but no rows under them."


def test_there_is_a_most_spreadsheets(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "TABLES_MOST", 1)
    assert client.post("/api/tables", json={"name": "One", "rows_csv": PRICES}).status_code == 200
    response = client.post("/api/tables", json={"name": "Two", "rows_csv": PRICES})
    assert response.status_code == 400 and "Delete one to add another" in response.json()["detail"]


def test_a_call_sees_the_spreadsheets_and_whose_rows_it_may_tell(cfg):
    from app.agent import caller_for, tables_for
    from app.store import Store

    store = Store(cfg.db_path)
    headings, rows = read_table(PRICES)
    store.add_lookup_table(name="Price list", description="", private=True, file_name="", headings=headings, rows=rows)
    (table,) = tables_for(store)
    assert (table.name, table.private, len(table.rows)) == ("Price list", True, 4)
    incoming = store.create_call("phone_inbound", status="live", from_number="+61412345678")
    outgoing = store.create_call("phone_outbound", status="live", to_number="+61498765432")
    browser = store.create_call("browser", status="live")
    assert caller_for(store, incoming["id"]) == "+61412345678"
    assert caller_for(store, outgoing["id"]) == "+61498765432"
    assert caller_for(store, browser["id"]) is None
    store.close()


def test_a_caller_is_only_given_rows_with_their_whole_number():
    rows = [
        ["Alice", "+1 312 555 0123", "Gate"],
        ["Bea", "(02) 9555 0147", "Fence"],  # Sydney, written the local way
        ["Cal", "0412 345 678 / 02 9555 0199", "Post"],  # two numbers in one cell
    ]
    table = Table(id="c", name="Customers", description="", headings=["Name", "Phone", "Order"], rows=rows, private=True)
    assert own_rows(table, "+13125550123") == [0]
    assert own_rows(table, "+12125550123") == []  # New York: the same last eight digits, someone else
    assert own_rows(table, "+61295550147") == [1]
    assert own_rows(table, "+61395550147") == []  # Melbourne: the same landline digits, another state
    assert own_rows(table, "+919195550147") == []  # India, ending the same way
    assert own_rows(table, "+61412345678") == [2]
    assert own_rows(table, "+61295550199") == [2]


def test_a_private_sheet_is_never_shown_by_example():
    table = orders(10, private=True)
    summary = tables_summary([table])
    assert "Columns: Order, Name, Phone, Status, Delivery" in summary
    assert "ORD-" not in summary and "+61" not in summary


def test_headings_are_not_taken_from_a_row_of_data():
    # Two note columns with no heading: the headings still head the sheet.
    headings, rows = read_table("Name,Phone,Order,,\nAlice,+61400000001,ORD-1,paid,urgent\nBob,+61400000002,ORD-2,,\n")
    assert headings[:3] == ["Name", "Phone", "Order"] and len(rows) == 2
    # One early row fuller than the headings (a note in two more cells): nothing is lost.
    headings, rows = read_table("Product,Price,Stock\nGate,$420,12,call first,ask Sam\nFence,$89,140\n")
    assert headings[:3] == ["Product", "Price", "Stock"] and [row[0] for row in rows] == ["Gate", "Fence"]
    # A title of one cell, or a CSV title with a comma in it, is passed over.
    assert read_table("Prices\nProduct,Price\nGate,$5\n")[0] == ["Product", "Price"]


def test_words_in_any_script_are_searched_whole():
    assert words_of("राहुल शर्मा | 9876543210") == ["राहुल", "शर्मा", "9876543210"]
    assert words_of("Crème brûlée") == ["creme", "brulee"]
    table = Table(id="h", name="Products", description="", headings=["Name", "Price"],
                  rows=[["गुलाब साबुन", "₹40"], ["नीम साबुन", "₹35"], ["Soap", "$2"]])
    assert matching_rows(table, "गुलाब साबुन")[0] == 0
