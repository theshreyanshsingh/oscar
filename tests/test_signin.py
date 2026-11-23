"""The dashboard's password (app/signin.py): signing in, the session cookie, what is refused
without one, the lock while there is no password, and sending http to https."""

import re
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app import signin
from app.config import ROOT_DIR
from app.main import create_app
from app.signin import COOKIE, FAILURES_ALLOWED, LOCKED, SESSION_SECONDS, SIGN_IN_FIRST, WRONG_PASSWORD, safe_next
from tests.conftest import DASHBOARD_PASSWORD, make_config, sign_in

CLOUDFLARE_PEER = ("162.158.10.20", 443)  # one of Cloudflare's addresses
SOMEONE = ("203.0.113.7", 50000)  # anyone else


def app_client(tmp_path, *, base_url="https://oscar.example.test", client=SOMEONE, cookies=None, **config) -> TestClient:
    """A client of a server Oscar (HOST=0.0.0.0) with the test password, not signed in."""
    cfg = make_config(tmp_path, host="0.0.0.0", **{"public_url": "", **config})
    return TestClient(create_app(cfg), base_url=base_url, client=client, cookies=cookies, follow_redirects=False)


def set_cookie(response) -> SimpleCookie:
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie


def attributes(response) -> set[str]:
    """The Set-Cookie header's attributes, lower case, values left out."""
    return {part.strip().split("=")[0].lower() for part in response.headers["set-cookie"].split(";")[1:]}


def try_password(client, password, **request):
    return client.post("/login", data={"password": password}, **request)


# Signing in


def test_the_right_password_signs_in_and_goes_where_you_were_going(tmp_path):
    with app_client(tmp_path) as client:
        page = client.get("/?call=abc")
        assert (page.status_code, page.headers["location"]) == (303, "/login?next=%2F%3Fcall%3Dabc")
        form = client.get(page.headers["location"])
        assert form.status_code == 200 and "Sign in" in form.text
        assert 'name="next" value="/?call=abc"' in form.text
        assert 'type="password"' in form.text and 'autocomplete="current-password"' in form.text

        signed_in = client.post("/login", data={"password": DASHBOARD_PASSWORD, "next": "/?call=abc"})
        assert (signed_in.status_code, signed_in.headers["location"]) == (303, "/?call=abc")
        assert client.get("/").status_code == 200
        assert client.get("/api/agent").status_code == 200
        # Already signed in: the sign-in page goes on to the dashboard.
        assert client.get("/login?next=/%3Fcall%3Dabc").headers["location"] == "/?call=abc"


@pytest.mark.parametrize("wrong", ["not-the-password", DASHBOARD_PASSWORD.upper(), DASHBOARD_PASSWORD + " ", "x" * 5000])
def test_a_wrong_password_is_refused_and_gives_no_session(tmp_path, wrong):
    with app_client(tmp_path) as client:
        refused = try_password(client, wrong)
        assert refused.status_code == 401
        assert WRONG_PASSWORD in refused.text
        assert "set-cookie" not in refused.headers
        assert client.get("/api/agent").status_code == 401


def test_no_password_typed_is_asked_for_and_not_counted(tmp_path):
    with app_client(tmp_path) as client:
        for _ in range(FAILURES_ALLOWED + 2):
            asked = try_password(client, "")
            assert asked.status_code == 400 and "Type the password." in asked.text
        assert try_password(client, DASHBOARD_PASSWORD).status_code == 303


def test_the_password_is_compared_in_constant_time(monkeypatch):
    compared = []
    real = signin.hmac.compare_digest
    monkeypatch.setattr(signin.hmac, "compare_digest", lambda a, b: compared.append((a, b)) or real(a, b))
    assert signin.password_matches("secret-password", "secret-password")
    assert not signin.password_matches("short", "secret-password")
    # Digests of the same length are compared, whatever the passwords' lengths.
    assert compared and all(len(a) == len(b) == 32 for a, b in compared)


@pytest.mark.parametrize(
    ("asked", "goes_to"),
    [
        ("/?call=abc#history", "/?call=abc#history"),
        ("/static/app.js", "/static/app.js"),
        ("//evil.example/x", "/"),
        ("https://evil.example/", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("/login?next=/", "/"),
        ("/logout", "/"),
        ("", "/"),
    ],
)
def test_after_signing_in_only_a_page_of_this_site_is_gone_to(asked, goes_to):
    assert safe_next(asked) == goes_to


def test_a_form_sent_from_another_site_is_refused(tmp_path):
    with app_client(tmp_path) as client:
        refused = try_password(client, DASHBOARD_PASSWORD, headers={"origin": "https://evil.example"})
        assert refused.status_code == 403 and "set-cookie" not in refused.headers
        # From Oscar's own page it works.
        assert try_password(client, DASHBOARD_PASSWORD, headers={"origin": "https://oscar.example.test"}).status_code == 303
        assert client.post("/logout", headers={"origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/agent").status_code == 200


# The cookie


def test_the_session_cookie_is_httponly_lax_and_lasts_thirty_days(tmp_path):
    with app_client(tmp_path) as client:
        signed_in = try_password(client, DASHBOARD_PASSWORD)
    cookie = set_cookie(signed_in)[COOKIE]
    assert cookie["httponly"] is True
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    assert int(cookie["max-age"]) == SESSION_SECONDS == 30 * 24 * 60 * 60
    assert cookie["secure"] is True  # signed in over https
    assert "domain" not in attributes(signed_in)  # this address only


@pytest.mark.parametrize(
    ("base_url", "headers", "secure"),
    [
        ("https://oscar.example.test", {}, True),  # straight over https
        ("http://oscar.example.test", {"cf-visitor": '{"scheme":"https"}'}, True),  # https as far as Cloudflare
        ("http://oscar.example.test", {}, True),  # PUBLIC_URL's address, which is https
        ("http://13.185.17.24:7860", {}, False),  # the server's own address, plain http
    ],
    ids=["https", "cloudflare https", "public url", "plain http"],
)
def test_the_cookie_is_secure_whenever_the_browser_came_over_https(tmp_path, base_url, headers, secure):
    with app_client(tmp_path, base_url=base_url, public_url="https://oscar.example.test") as client:
        signed_in = try_password(client, DASHBOARD_PASSWORD, headers=headers)
    assert signed_in.status_code == 303
    assert ("secure" in attributes(signed_in)) is secure
    assert {"httponly", "samesite", "max-age", "path"} <= attributes(signed_in)


def test_a_made_up_or_altered_cookie_is_refused(tmp_path):
    with app_client(tmp_path) as client:
        real = set_cookie(try_password(client, DASHBOARD_PASSWORD))[COOKIE].value
        session_id, expires, signature = real.split(".")
        client.cookies.clear()
        for forged in [
            f"{session_id}.{int(expires) + 999_999}.{signature}",  # kept longer
            f"{session_id[:-1]}A.{expires}.{signature}",  # someone else's session
            f"{session_id}.{expires}.{'0' * 64}",
            f"{session_id}.{expires}",
            "anything",
        ]:
            client.cookies.set(COOKIE, forged)
            assert client.get("/api/agent").status_code == 401, forged
        client.cookies.set(COOKIE, real)
        assert client.get("/api/agent").status_code == 200


def test_a_session_ends_after_thirty_days(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(signin, "wall_clock", lambda: now[0])
    with app_client(tmp_path) as client:
        cookie = set_cookie(try_password(client, DASHBOARD_PASSWORD))[COOKIE].value
        # Kept by a browser that ignores the cookie's own expiry: Oscar itself refuses it.
        client.cookies.clear()
        client.cookies.set(COOKIE, cookie)
        now[0] += SESSION_SECONDS - 60
        assert client.get("/api/agent").status_code == 200
        now[0] += 120
        assert client.get("/api/agent").status_code == 401


def test_changing_the_password_ends_every_session(tmp_path):
    with app_client(tmp_path) as client:
        cookie = set_cookie(try_password(client, DASHBOARD_PASSWORD))[COOKIE].value
    # Restarted with the same password: still signed in (the secret is kept in the database).
    with app_client(tmp_path, cookies={COOKIE: cookie}) as client:
        assert client.get("/api/agent").status_code == 200
    # Restarted with a new password: signed out.
    with app_client(tmp_path, dashboard_password="a-brand-new-password", cookies={COOKIE: cookie}) as client:
        assert client.get("/api/agent").status_code == 401
        assert client.get("/").headers["location"] == "/login"
        assert try_password(client, DASHBOARD_PASSWORD).status_code == 401
        assert try_password(client, "a-brand-new-password").status_code == 303
        assert client.get("/api/agent").status_code == 200


def test_sign_out_ends_the_session_even_if_its_cookie_is_kept(tmp_path):
    with app_client(tmp_path) as client:
        cookie = set_cookie(try_password(client, DASHBOARD_PASSWORD))[COOKIE].value
        other = TestClient(client.app, base_url="https://oscar.example.test", client=SOMEONE)
        sign_in(other)  # another browser
        out = client.post("/logout")
        assert (out.status_code, out.headers["location"]) == (303, "/login?signed_out=1")
        assert set_cookie(out)[COOKIE]["max-age"] == "0"
        assert "signed out" in client.get("/login?signed_out=1").text
        client.cookies.set(COOKIE, cookie)  # replayed
        assert client.get("/api/agent").status_code == 401
        assert other.get("/api/agent").status_code == 200  # only that browser was signed out
    # And after a restart.
    with app_client(tmp_path, cookies={COOKIE: cookie}) as client:
        assert client.get("/api/agent").status_code == 401


# Too many wrong passwords


def test_ten_wrong_passwords_make_that_address_wait(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(signin, "steady_clock", lambda: clock[0])
    with app_client(tmp_path) as client:
        for tried in range(FAILURES_ALLOWED):
            refused = try_password(client, f"guess-{tried}")
            assert refused.status_code == 401
        waiting = try_password(client, DASHBOARD_PASSWORD)  # even the right one
        assert waiting.status_code == 429
        assert "Too many wrong passwords from your connection. Try again in 15 minutes." in waiting.text
        assert int(waiting.headers["retry-after"]) == 15 * 60
        assert "set-cookie" not in waiting.headers

        # Someone else is not held up by it.
        other = TestClient(client.app, base_url="https://oscar.example.test", client=("198.51.100.9", 1))
        assert try_password(other, DASHBOARD_PASSWORD, follow_redirects=False).status_code == 303

        clock[0] += 15 * 60 + 1
        assert try_password(client, DASHBOARD_PASSWORD).status_code == 303


def test_the_last_tries_are_counted_down(tmp_path):
    with app_client(tmp_path) as client:
        said = [try_password(client, f"guess-{n}").text for n in range(FAILURES_ALLOWED)]
    assert "more tries" not in said[0]
    assert "3 more tries, then sign-in waits 15 minutes." in said[-4]
    assert "1 more try, then sign-in waits 15 minutes." in said[-2]
    assert "Sign-in from your connection now waits 15 minutes." in said[-1]


def test_signing_in_clears_the_count(tmp_path):
    with app_client(tmp_path) as client:
        for n in range(FAILURES_ALLOWED - 1):
            try_password(client, f"guess-{n}")
        assert try_password(client, DASHBOARD_PASSWORD).status_code == 303
        for n in range(FAILURES_ALLOWED - 1):
            assert try_password(client, f"again-{n}").status_code == 401
        assert try_password(client, DASHBOARD_PASSWORD).status_code == 303


def test_through_cloudflare_each_visitor_is_counted_by_their_own_address(tmp_path):
    with app_client(tmp_path, client=CLOUDFLARE_PEER) as client:
        for n in range(FAILURES_ALLOWED):
            try_password(client, f"guess-{n}", headers={"cf-connecting-ip": "192.0.2.1"})
        assert try_password(client, DASHBOARD_PASSWORD, headers={"cf-connecting-ip": "192.0.2.1"}).status_code == 429
        # Another visitor, through the same Cloudflare address.
        assert try_password(client, DASHBOARD_PASSWORD, headers={"cf-connecting-ip": "192.0.2.2"}).status_code == 303


def test_a_made_up_visitor_address_from_outside_cloudflare_counts_for_the_real_one(tmp_path):
    # Straight at the server's port, anyone can write CF-Connecting-IP: it is not believed.
    with app_client(tmp_path, client=SOMEONE) as client:
        for n in range(FAILURES_ALLOWED):
            try_password(client, f"guess-{n}", headers={"cf-connecting-ip": f"192.0.2.{n + 1}"})
        assert try_password(client, DASHBOARD_PASSWORD, headers={"cf-connecting-ip": "192.0.2.200"}).status_code == 429


def test_the_addresses_counted_cannot_grow_without_end(monkeypatch):
    monkeypatch.setattr(signin, "ADDRESSES_REMEMBERED", 50)
    failures = signin.FailedSignIns()
    for n in range(500):
        failures.failed(f"10.0.{n // 250}.{n % 250}")
    assert len(failures._failures) == 50


def test_ipv6_visitors_are_counted_by_their_network():
    assert signin.sign_in_key("2001:db8:1:2:aaaa::1") == signin.sign_in_key("2001:db8:1:2:bbbb::9")
    assert signin.sign_in_key("2001:db8:1:3::1") != signin.sign_in_key("2001:db8:1:2::1")
    assert signin.sign_in_key("203.0.113.7") == "203.0.113.7"


# What needs a session


def every_route(app) -> list[tuple[str, str]]:
    """(method, path) of every route the app has, path parameters filled in. The routers
    it includes are the sign-in's (/login, /logout) and Twilio's (/twilio/), both open."""
    found = []
    included = [route for route in app.routes if not hasattr(route, "path")]
    assert len(included) == 2, included
    for route in app.routes:
        if route in included:
            continue
        if isinstance(route, Mount):
            found.append(("GET", "/static/app.js"))
            continue
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        if isinstance(route, WebSocketRoute):
            found.append(("WEBSOCKET", path))
        elif isinstance(route, Route):
            found.extend((method, path) for method in sorted(route.methods - {"HEAD"}))
    return found


OPEN = {"/healthz", "/login", "/logout"}


def test_every_page_api_route_and_websocket_is_refused_without_a_session(tmp_path):
    with app_client(tmp_path) as client:
        routes = every_route(client.app)
        assert ("POST", "/api/calls/browser") in routes  # the WebRTC offer
        assert ("GET", "/api/calls.csv") in routes and ("GET", "/api/spending") in routes
        checked = 0
        for method, path in routes:
            if path in OPEN or path.startswith("/twilio/"):
                continue
            checked += 1
            if method == "WEBSOCKET":
                with pytest.raises(WebSocketDisconnect) as closed:
                    with client.websocket_connect(path):
                        pass
                assert closed.value.code == 1008, path
                continue
            response = client.request(method, path, json={})
            if path.startswith("/api/") or method != "GET":
                assert response.status_code == 401, (method, path)
                assert response.json() == {"detail": SIGN_IN_FIRST, "sign_in": "/login"}, (method, path)
            else:
                assert response.status_code == 303 and response.headers["location"].startswith("/login"), path
        assert checked > 40


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/api/calls"),
        ("GET", "/api/calls.csv"),
        ("GET", "/api/calls/abc"),
        ("GET", "/api/calls/abc/recording"),
        ("GET", "/api/spending"),
        ("GET", "/api/status"),
        ("GET", "/api/agent"),
        ("PUT", "/api/agent"),
        ("POST", "/api/calls/browser"),
        ("POST", "/api/calls/phone"),
        ("GET", "/api/events"),
        ("GET", "/api/whatsapp/qr.svg"),
        ("GET", "/api/no-such-thing"),
        ("GET", "/static/app.js"),
        ("GET", "/static/index.html"),
        ("GET", "/no-such-page"),
    ],
)
def test_the_things_qa_found_open_are_refused(tmp_path, method, path):
    with app_client(tmp_path) as client:
        response = client.request(method, path, json={"sdp": "v=0", "type": "offer"} if method == "POST" else None)
    assert response.status_code in (401, 303), response.text
    assert "call history" not in response.text.lower()


def test_the_webrtc_offer_is_refused_before_any_call_is_made(tmp_path):
    with app_client(tmp_path) as client:
        refused = client.post("/api/calls/browser", json={"sdp": "v=0\r\n", "type": "offer"})
        assert refused.status_code == 401
        assert client.app.state.store.find_calls(limit=10)[0] == []


def test_a_websocket_needs_a_session(tmp_path):
    with app_client(tmp_path) as client:

        async def live(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("hello")
            await websocket.close()

        client.app.add_api_websocket_route("/api/live", live)
        for path in ["/api/live", "/api/anything", "/"]:
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect(path):
                    pass
            assert closed.value.code == 1008, path
        cookie = set_cookie(try_password(client, DASHBOARD_PASSWORD))[COOKIE].value
        # (The test client's WebSockets go to another host name, so the cookie is given by hand.)
        with client.websocket_connect("/api/live", headers={"cookie": f"{COOKIE}={cookie}"}) as websocket:
            assert websocket.receive_text() == "hello"
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/api/live", headers={"cookie": f"{COOKIE}={cookie}x"}):
                pass
        assert closed.value.code == 1008


def test_the_open_paths_answer_without_a_session(tmp_path):
    with app_client(tmp_path) as client:
        assert client.get("/healthz").json() == {"ok": True, "calls": 0}
        # Reached Twilio's own signature check, not the sign-in.
        voice = client.post("/twilio/voice", data={"From": "+14155550123"})
        assert voice.status_code == 403 and "Sign in" not in voice.text
        reached = []
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/twilio/media") as websocket:
                reached.append(True)  # accepted: the sign-in let it through
                websocket.send_text('{"event": "start", "start": {"customParameters": {}}}')
                websocket.receive_text()
        assert reached and closed.value.code == 1008  # then Oscar's own refusal: no token for a call
        assert client.get("/login").status_code == 200
        assert client.get("/static/style.css").status_code == 200
        assert client.get("/static/signin.js").status_code == 200
        out = client.post("/logout")
        assert (out.status_code, out.headers["location"]) == (303, "/login?signed_out=1")


def test_the_status_tells_the_dashboard_to_show_sign_out(tmp_path):
    with app_client(tmp_path) as client:
        sign_in(client)
        assert client.get("/api/status").json()["dashboard"] == {"password": True, "cloudflare_access": False}


def test_the_dashboard_goes_to_the_sign_in_page_when_refused():
    # The dashboard's own JavaScript: a 401 (or the lock) from any API call sends the page to /login.
    script = (ROOT_DIR / "app" / "static" / "app.js").read_text()
    assert 'return status === 401 || (status === 503 && Boolean(body && body.locked));' in script
    api = script[script.index("async function api(") : script.index("function describeError(")]
    assert "if (needsSignIn(response.status, body)) {\n    goToSignIn();" in api
    assert script.count("needsSignIn(response.status, body)") == 2  # api() and the voice sample
    assert 'location.assign(here === "/" ? "/login" : `/login?next=${encodeURIComponent(here)}`);' in script
    page = (ROOT_DIR / "app" / "static" / "index.html").read_text()
    assert '<form class="sign-out" id="sign-out" method="post" action="/logout" hidden>' in page


# Locked: reachable from elsewhere, with no password


@pytest.mark.parametrize(
    "where",
    [{"host": "0.0.0.0"}, {"host": "127.0.0.1", "public_url": "https://abc.ngrok-free.app"}],
    ids=["server", "mac with a tunnel"],
)
def test_without_a_password_a_reachable_oscar_starts_but_is_locked(tmp_path, where):
    cfg = make_config(tmp_path, dashboard_password="", **{"public_url": "", **where})
    assert cfg.dashboard_mode == "locked"
    with TestClient(create_app(cfg), base_url="http://localhost", follow_redirects=False) as client:
        for path in ["/", "/static/app.js", "/login", "/?call=abc"]:
            page = client.get(path)
            assert page.status_code == 503, path
            assert "Set a password first" in page.text
            assert ".venv/bin/python -m scripts.set_keys --password" in page.text
        for method, path in [("GET", "/api/calls"), ("GET", "/api/calls.csv"), ("GET", "/api/spending"),
                             ("PUT", "/api/agent"), ("POST", "/api/calls/browser")]:
            refused = client.request(method, path, json={})
            assert refused.status_code == 503, path
            assert refused.json() == {"detail": LOCKED, "locked": True, "sign_in": "/login"}
        # No password to guess.
        assert try_password(client, "").status_code == 503
        assert try_password(client, DASHBOARD_PASSWORD).status_code == 503
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("ws://localhost/api/anything"):
                pass
        assert closed.value.code == 1008
        assert client.get("/healthz").json() == {"ok": True, "calls": 0}
        assert client.post("/twilio/voice", data={}).status_code == 403  # Twilio's signature check
        assert client.get("/static/style.css").status_code == 200


def test_on_a_mac_only_it_can_reach_the_dashboard_opens_without_a_password_as_before(tmp_path):
    cfg = make_config(tmp_path, dashboard_password="", public_url="")
    assert cfg.dashboard_mode == "open"
    with TestClient(create_app(cfg), base_url="http://localhost", follow_redirects=False) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/agent").status_code == 200
        assert client.get("/api/status").json()["dashboard"]["password"] is False
        assert client.get("/login").headers["location"] == "/"
        # The Host check still keeps other sites out.
        assert client.get("/api/agent", headers={"host": "attacker.example"}).status_code == 400


def test_on_a_mac_with_a_password_it_is_asked_for(tmp_path):
    cfg = make_config(tmp_path, public_url="")
    assert cfg.dashboard_mode == "password"
    with TestClient(create_app(cfg), base_url="http://localhost", follow_redirects=False) as client:
        assert client.get("/api/agent").status_code == 401
        sign_in(client)
        assert client.get("/api/agent").status_code == 200


# http to https


@pytest.mark.parametrize(
    ("path", "goes_to"),
    [
        ("/", "https://oscar.example.com/"),
        ("/api/calls?q=sam&show=todo", "https://oscar.example.com/api/calls?q=sam&show=todo"),
        ("/login?next=%2F%3Fcall%3Dabc", "https://oscar.example.com/login?next=%2F%3Fcall%3Dabc"),
    ],
)
def test_a_visit_cloudflare_got_over_http_goes_to_https(tmp_path, path, goes_to):
    with app_client(tmp_path, base_url="http://oscar.example.com", client=CLOUDFLARE_PEER) as client:
        response = client.get(path, headers={"cf-visitor": '{"scheme":"http"}'})
        assert (response.status_code, response.headers["location"]) == (308, goes_to)
        assert client.post("/login", data={"password": DASHBOARD_PASSWORD}, headers={"cf-visitor": '{"scheme":"http"}'}).status_code == 308


def test_only_cloudflares_header_sends_to_https(tmp_path):
    with app_client(tmp_path, base_url="http://oscar.example.com") as client:
        assert client.get("/", headers={"cf-visitor": '{"scheme":"https"}'}).status_code == 303  # to /login
        assert client.get("/", headers={"x-forwarded-proto": "http"}).status_code == 303
        assert client.get("/").status_code == 303
        # The health check and Twilio's webhooks answer over http too.
        assert client.get("/healthz", headers={"cf-visitor": '{"scheme":"http"}'}).status_code == 200
        voice = client.post("/twilio/voice", data={}, headers={"cf-visitor": '{"scheme":"http"}'})
        assert voice.status_code == 403
        # A Host that is not one is not sent anywhere.
        odd = client.get("/", headers={"cf-visitor": '{"scheme":"http"}', "host": "evil.example/x"})
        assert odd.status_code == 400


# Nothing about the server's insides where visitors can see it


SERVER_INSIDES = re.compile(r"\budp\b|security group|firewall|\bports?\b \d|ip_local_port_range", re.IGNORECASE)


def test_the_dashboard_and_sign_in_pages_never_name_the_servers_ports_or_firewall():
    for page in [*(ROOT_DIR / "app" / "static").glob("*"), *(ROOT_DIR / "app" / "pages").glob("*")]:
        found = SERVER_INSIDES.search(page.read_text())
        assert found is None, f"{page.name}: {found.group(0) if found else ''}"


def test_messages_visitors_can_see_never_name_the_servers_ports_or_firewall(tmp_path):
    from app.config import call_audio_unreachable

    for where in [{"host": "0.0.0.0"}, {"host": "127.0.0.1"}]:
        assert SERVER_INSIDES.search(call_audio_unreachable(make_config(tmp_path, **where))) is None
    with app_client(tmp_path) as client:
        for response in [client.get("/healthz"), client.get("/api/status"), client.get("/login"), client.get("/")]:
            assert SERVER_INSIDES.search(response.text) is None
        sign_in(client)
        assert SERVER_INSIDES.search(client.get("/api/status").text) is None


def test_the_sign_in_page_is_not_cached_or_framed(tmp_path):
    with app_client(tmp_path) as client:
        page = client.get("/login")
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["content-type"].startswith("text/html")
    assert Path(ROOT_DIR / "app" / "pages" / "signin.html").exists()
