"""Deleting a call, exporting Call history, what calls cost by day, and OpenRouter's models."""

import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.catalog import ModelCatalog
from app.main import create_app, spreadsheet_safe
from tests.conftest import make_config


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="http://localhost") as test_client:
        yield test_client


def a_call(client, *, status="ended", said=("Hi, it's Sam.",), **fields) -> dict:
    store = client.app.state.store
    call = store.create_call("phone_inbound", status="live", from_number="+61412345678")
    for text in said:
        store.add_message(call["id"], "user", text)
    store.add_search(call["id"], "fence prices", [{"title": "Fences", "url": "https://x.test", "excerpt": "..."}])
    return store.update_call(call["id"], status=status, **fields)


# Deleting a call


def test_a_call_is_deleted_with_its_words_and_searches(client):
    store = client.app.state.store
    call = a_call(client, summary="Sam wants a fence.", follow_up="Call Sam back.")
    kept = a_call(client)
    assert client.delete(f"/api/calls/{call['id']}").json() == {"deleted": call["id"]}
    assert client.get(f"/api/calls/{call['id']}").status_code == 404
    assert store.get_messages(call["id"]) == [] and store.get_searches(call["id"]) == []
    assert [c["id"] for c in client.get("/api/calls").json()["calls"]] == [kept["id"]]
    assert client.get("/api/calls").json()["open_todos"] == 0
    assert len(store.get_messages(kept["id"])) == 1  # the other call is untouched


def test_a_call_that_is_on_or_being_summed_up_is_not_deleted(client):
    live = a_call(client, status="live")
    response = client.delete(f"/api/calls/{live['id']}")
    assert response.status_code == 409 and "End the call" in response.json()["detail"]
    writing = a_call(client, summary_status="writing")
    assert client.delete(f"/api/calls/{writing['id']}").status_code == 409
    assert client.delete("/api/calls/nope").status_code == 404
    assert client.get(f"/api/calls/{live['id']}").status_code == 200


def test_other_dashboards_are_told_a_call_was_deleted(client):
    call = a_call(client)
    queue = client.app.state.calls.bus.subscribe()
    client.delete(f"/api/calls/{call['id']}")
    assert queue.get_nowait() == {"type": "call_deleted", "id": call["id"]}


# Exporting Call history


def rows_of(response) -> list[dict]:
    text = response.content.decode("utf-8")
    assert text.startswith("﻿")  # so Excel reads it as UTF-8
    return list(csv.DictReader(io.StringIO(text[1:])))


def test_call_history_is_exported_as_a_spreadsheet(client):
    a_call(client, summary="Sam wants a fence.", caller_name="Sam", callback_number="0412345678", follow_up="Ring Sam.")
    a_call(client, summary="राहुल wants a table.")
    response = client.get("/api/calls.csv")
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"].startswith('attachment; filename="oscar-calls-')
    rows = rows_of(response)
    assert [row["Summary"] for row in rows] == ["राहुल wants a table.", "Sam wants a fence."]  # newest first
    assert rows[1]["From"] == "+61412345678" and rows[1]["Call back on"] == "0412345678"
    assert rows[1]["To do"] == "Ring Sam." and rows[1]["Type"] == "phone_inbound"


def test_the_export_holds_what_the_search_and_filter_show(client):
    a_call(client, summary="Sam wants a fence.", follow_up="Ring Sam.")
    a_call(client, summary="Jo asked about hours.")
    assert [row["Summary"] for row in rows_of(client.get("/api/calls.csv", params={"q": "fence"}))] == ["Sam wants a fence."]
    assert [row["Summary"] for row in rows_of(client.get("/api/calls.csv", params={"show": "todo"}))] == ["Sam wants a fence."]


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        ("=HYPERLINK(\"https://evil.test\")", "'=HYPERLINK(\"https://evil.test\")"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("-2+3", "'-2+3"),
        ("+61412345678", "+61412345678"),  # a phone number stays a phone number
        ("-5", "-5"),
        (0.031, "0.031"),
        (None, ""),
        ("Sam wants a fence.", "Sam wants a fence."),
    ],
)
def test_nothing_a_caller_said_runs_as_a_formula(value, cell):
    assert spreadsheet_safe(value) == cell


# What calls cost, day by day


def test_spending_lists_the_recent_calls_that_cost_something(client):
    store = client.app.state.store
    paid = a_call(client)
    store.add_call_cost(paid["id"], 0.0312)
    a_call(client)  # cost nothing
    old = a_call(client)
    store.add_call_cost(old["id"], 1.0)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(timespec="seconds")
    store._db.execute("UPDATE calls SET started_at = ? WHERE id = ?", (long_ago, old["id"]))
    recent = client.get("/api/spending").json()["recent_calls"]
    assert recent == [[paid["started_at"], 0.0312]]


# OpenRouter's models


def listing(*entries, status=200, asked=None):
    def respond(request):
        if asked is not None:
            asked.append(request)
        return httpx.Response(status, json={"data": list(entries)})

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


GPT = {"id": "openai/gpt-5", "name": "OpenAI: GPT-5", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}}
HAIKU = {"id": "anthropic/claude-haiku-5.5", "name": "Anthropic: Claude Haiku 5.5"}
PAINTER = {"id": "x/painter", "name": "Painter", "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]}}


async def test_the_chat_models_are_listed_by_id_and_kept_a_while():
    asked = []
    catalog = ModelCatalog(base_url="https://or.test/api/v1", client=listing(GPT, PAINTER, HAIKU, {"name": "no id"}, asked=asked))
    models = await catalog.get()
    assert models == [
        {"id": "anthropic/claude-haiku-5.5", "name": "Anthropic: Claude Haiku 5.5"},
        {"id": "openai/gpt-5", "name": "OpenAI: GPT-5"},
    ]
    assert await catalog.get() == models and len(asked) == 1
    assert str(asked[0].url) == "https://or.test/api/v1/models" and "authorization" not in asked[0].headers


async def test_a_list_that_cannot_be_had_is_empty_and_not_asked_for_on_every_load():
    asked = []
    catalog = ModelCatalog(base_url="https://or.test/api/v1", client=listing(status=503, asked=asked))
    assert await catalog.get() == [] and await catalog.get() == []
    assert len(asked) == 1


def test_the_dashboard_gets_the_models(tmp_path):
    catalog = ModelCatalog(base_url="https://or.test/api/v1", client=listing(HAIKU))
    with TestClient(create_app(make_config(tmp_path), catalog=catalog), base_url="http://localhost") as client:
        assert client.get("/api/models").json() == {"models": [{"id": HAIKU["id"], "name": HAIKU["name"]}]}
