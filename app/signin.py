"""The dashboard's password: the sign-in page, the session it gives, and what is refused without one.

DASHBOARD_PASSWORD in .env (`.venv/bin/python -m scripts.set_keys --password`)
is the dashboard's protection. What Oscar does depends on it and on who can
reach Oscar (`Config.dashboard_mode`):

- "password": a password is set. Every page, /api/* route, WebSocket and
  browser-call offer needs a session, which signing in at /login gives: a
  signed cookie that lasts 30 days. Pages without one go to /login, API calls
  get 401 and WebSockets are closed with 1008. Changing the password ends every
  session; Sign out ends the one it is pressed in.
- "locked": no password, but other computers can reach Oscar (HOST is not
  127.0.0.1 or localhost, or PUBLIC_URL is set). Everything is refused, and
  pages say how to set a password. Oscar still starts (deploy.sh puts back the
  code before when new code does not start, which would leave an open Oscar
  running).
- "open": no password, and only this computer can reach Oscar (the Mac set-up,
  HOST=127.0.0.1 without PUBLIC_URL): no sign-in, as before.

Always open: /healthz (deploy.sh and scripts/autostart.py ask it whether Oscar
is up), Twilio's webhooks under /twilio/ (each checked by Twilio's signature,
or by a one-time token for a call's audio), and the sign-in page with the two
files it needs.

Cloudflare Access (app/access.py) is an extra layer: when it is set up, a
visitor needs both its sign-in and this one.

A request Cloudflare received over plain http (its cf-visitor header says
"http") is sent to the same address over https (HttpsOnly), and every answer to
a request that came over https tells the browser to use only https here for a
year (Strict-Transport-Security).
"""

import hashlib
import hmac
import html
import ipaddress
import json
import math
import re
import secrets
import time
from collections import deque
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Request
from loguru import logger
from starlette.requests import cookie_parser
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from app.access import is_open
from app.config import Config

COOKIE = "oscar_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
# Wrong passwords allowed from one address in FAILURE_WINDOW_SECONDS; then it waits.
FAILURES_ALLOWED = 10
FAILURE_WINDOW_SECONDS = 15 * 60
# Wrong passwords from everyone together in that time, after which every sign-in waits. Behind
# a proxy on the same machine every visitor has the proxy's address, and a visitor can name
# any address they like in X-Forwarded-For (which Oscar does not believe): guessing stays
# slow however the addresses come.
EVERYONE = "*"
EVERYONE_FAILURES_ALLOWED = 100
# Addresses whose wrong passwords are remembered at once, so a flood of them cannot fill the memory.
ADDRESSES_REMEMBERED = 10_000
# Said once this few tries are left before sign-in waits.
TRIES_LEFT_SHOWN = 3
FORM_MOST_BYTES = 8 * 1024

SIGN_IN_PATH = "/login"
SIGN_OUT_PATH = "/logout"
# What the sign-in page loads, so it opens before anyone has signed in.
SIGN_IN_FILES = ("/static/style.css", "/static/signin.js")

SET_PASSWORD = ".venv/bin/python -m scripts.set_keys --password"

SIGN_IN_FIRST = "Sign in to Oscar first."
# Said to anyone, so it names no command or file on the server: deploy.sh and the server log say how to set one.
LOCKED = "Oscar is locked until its dashboard has a password. Whoever runs this Oscar sets one on its server."
WRONG_PASSWORD = "That password is not right."
NO_PASSWORD_TYPED = "Type the password."
NOT_FROM_HERE = "This form has to be sent from Oscar's own page. Open Oscar's address and try again."

PAGE = Template((Path(__file__).resolve().parent / "pages" / "signin.html").read_text())


def _style_version() -> str:
    """The dashboard's own stylesheet version (in index.html), so the sign-in page never
    asks for an older one a browser may still have."""
    index = (Path(__file__).resolve().parent / "static" / "index.html").read_text()
    found = re.search(r"/static/style\.css\?v=(\d+)", index)
    return found.group(1) if found else "1"


STYLE_VERSION = _style_version()

# Cloudflare's addresses (https://www.cloudflare.com/ips/). A request from one of them came through
# Cloudflare, so its CF-Connecting-IP header says who sent it; anyone else could write that header.
CLOUDFLARE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


# Clocks, looked up when used (so tests can move them)


def wall_clock() -> float:
    """Unix time: when a session ends."""
    return time.time()


def steady_clock() -> float:
    """Seconds that only go forward: how long ago a wrong password was typed."""
    return time.monotonic()


# Who is asking, and how


def header(scope: Scope, name: bytes) -> str:
    """The first value of a request header ("" when there is none)."""
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return ""


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def from_cloudflare(peer: str) -> bool:
    """Whether a connection came from one of Cloudflare's addresses."""
    address = _address(peer)
    return address is not None and any(address in network for network in CLOUDFLARE_NETWORKS)


def client_address(scope: Scope) -> str:
    """Who sent a request: the address that connected, or, when that is Cloudflare, the visitor it names."""
    peer = (scope.get("client") or ("", 0))[0] or ""
    if from_cloudflare(peer):
        visitor = _address(header(scope, b"cf-connecting-ip"))
        if visitor is not None:
            return str(visitor)
    return peer or "unknown"


def sign_in_key(address: str) -> str:
    """Whose wrong passwords count together: one address, or one /64 for IPv6 (a home or a server has a whole one)."""
    parsed = _address(address)
    if isinstance(parsed, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return address


def cf_visitor_scheme(scope: Scope) -> str:
    """How the visitor reached Cloudflare ("http" or "https"), from its cf-visitor header; "" without one."""
    raw = header(scope, b"cf-visitor")
    if not raw:
        return ""
    try:
        scheme = json.loads(raw).get("scheme")
    except (ValueError, AttributeError):
        return ""
    return scheme.lower() if isinstance(scheme, str) else ""


def host_of(scope: Scope) -> str:
    """The Host the request was sent to, without its port, in lower case."""
    host = header(scope, b"host").strip().lower()
    if host.startswith("["):  # an IPv6 address: [::1]:7860
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def over_https(scope: Scope, public_host: str) -> bool:
    """Whether the visitor's browser reached Oscar over https: straight, through Cloudflare, or at PUBLIC_URL's address."""
    if scope.get("scheme") in ("https", "wss") or cf_visitor_scheme(scope) == "https":
        return True
    return bool(public_host) and host_of(scope) == public_host


# Sending http to https


VALID_HOST = re.compile(r"[A-Za-z0-9.-]+(:\d{1,5})?|\[[0-9A-Fa-f:.]+\](:\d{1,5})?")


# What the browser is told on an answer it got over https: use https here for the next year.
HSTS = (b"strict-transport-security", b"max-age=31536000")


class HttpsOnly:
    """Sends a request that reached Cloudflare over plain http to the same address over https (308).

    Only Cloudflare's own cf-visitor header is trusted for this, never
    X-Forwarded-Proto. /healthz and Twilio's webhooks answer either way.
    An answer to a request that came over https carries Strict-Transport-Security
    (browsers ignore it over http, so it isn't sent there).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not is_open(scope["path"]) and cf_visitor_scheme(scope) == "http":
            host = header(scope, b"host").strip()
            if not VALID_HOST.fullmatch(host):
                await PlainTextResponse("Bad request.", status_code=400)(scope, receive, send)
                return
            host = host.removesuffix(":80")
            raw_path = scope.get("raw_path")
            path = raw_path.decode("latin-1") if raw_path else quote(scope["path"])
            query = scope.get("query_string", b"").decode("latin-1")
            url = f"https://{host}{path}" + (f"?{query}" if query else "")
            await RedirectResponse(url, status_code=308)(scope, receive, send)
            return
        if scope["type"] == "http" and (scope.get("scheme") == "https" or cf_visitor_scheme(scope) == "https"):
            await self.app(scope, receive, with_hsts(send))
            return
        await self.app(scope, receive, send)


def with_hsts(send: Send) -> Send:
    """`send`, with Strict-Transport-Security added to the answer unless it already has one."""

    async def sending(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            if not any(name.lower() == HSTS[0] for name, _ in headers):
                headers.append(HSTS)
            message = {**message, "headers": headers}
        await send(message)

    return sending


# Sessions


def password_matches(given: str, password: str) -> bool:
    """Compared in constant time, whatever either one's length."""
    return hmac.compare_digest(
        hashlib.sha256(given.encode("utf-8", "surrogatepass")).digest(),
        hashlib.sha256(password.encode("utf-8", "surrogatepass")).digest(),
    )


class Sessions:
    """Signed session cookies: "<id>.<expires>.<signature>".

    The signature is an HMAC whose key comes from a random secret kept in the
    database and from the password, so a cookie cannot be made without the
    secret, and changing DASHBOARD_PASSWORD ends every session. A session
    signed out is remembered until it would have expired.
    """

    def __init__(self, password: str, store):
        self._store = store
        secret = store.dashboard_secret().encode()
        self._key = hmac.new(secret, b"oscar dashboard session\x00" + password.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()
        self._signed_out = store.signed_out_sessions(int(wall_clock()))

    def _signature(self, session_id: str, expires: int) -> str:
        digest = hmac.new(self._key, f"{session_id}.{expires}".encode(), hashlib.sha256).digest()
        return digest.hex()

    def new(self) -> str:
        session_id = secrets.token_urlsafe(18)
        expires = int(wall_clock()) + SESSION_SECONDS
        return f"{session_id}.{expires}.{self._signature(session_id, expires)}"

    def check(self, cookie: str) -> tuple[str, int] | None:
        """(id, expires) of a valid session; None for anything else."""
        parts = cookie.split(".")
        if len(parts) != 3:
            return None
        session_id, expires_text, signature = parts
        if not (re.fullmatch(r"[A-Za-z0-9_-]{16,64}", session_id) and re.fullmatch(r"\d{1,12}", expires_text)):
            return None
        expires = int(expires_text)
        if not hmac.compare_digest(signature.encode("utf-8", "replace"), self._signature(session_id, expires).encode()):
            return None
        if expires <= wall_clock() or session_id in self._signed_out:
            return None
        return session_id, expires

    def sign_out(self, session_id: str, expires: int) -> None:
        self._signed_out.add(session_id)
        self._store.sign_out_session(session_id, expires, int(wall_clock()))


class FailedSignIns:
    """Wrong passwords per address: after FAILURES_ALLOWED in FAILURE_WINDOW_SECONDS, that address waits."""

    def __init__(self):
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        times = self._failures.get(key) or deque()
        while times and times[0] <= steady_clock() - FAILURE_WINDOW_SECONDS:
            times.popleft()
        return times

    def wait_seconds(self, key: str, allowed: int = FAILURES_ALLOWED) -> int:
        """How long `key` must wait before trying again (0: it may try now)."""
        times = self._recent(key)
        if len(times) < allowed:
            return 0
        return max(1, math.ceil(times[-allowed] + FAILURE_WINDOW_SECONDS - steady_clock()))

    def failed(self, key: str, allowed: int = FAILURES_ALLOWED) -> int:
        """Counts a wrong password; says how many tries are left before `key` has to wait."""
        times = self._recent(key)
        times.append(steady_clock())
        self._failures.pop(key, None)
        self._failures[key] = times  # the most recent last, so the oldest go first below
        if len(self._failures) > ADDRESSES_REMEMBERED:
            for old in list(self._failures)[: len(self._failures) - ADDRESSES_REMEMBERED]:
                del self._failures[old]
        return max(0, allowed - len(times))

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)


# Pages


def _page(title: str, card: str, *, status: int, headers: dict | None = None) -> HTMLResponse:
    body = PAGE.substitute(title=html.escape(title), card=card, style_version=STYLE_VERSION)
    return HTMLResponse(
        body,
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin", **(headers or {})},
    )


def sign_in_page(*, next_path: str = "/", message: str = "", note: str = "", status: int = 200, headers=None) -> HTMLResponse:
    """The sign-in form. `message` says what went wrong; `note` is news that is not a problem."""
    shown = ""
    if note:
        shown = f'<p class="signin-note" role="status">{html.escape(note)}</p>'
    if message:
        shown = f'<p class="signin-message" id="signin-message" role="alert">{html.escape(message)}</p>'
    described = ' aria-describedby="signin-message" aria-invalid="true"' if message else ""
    card = f"""
      <p class="eyebrow signin-rise">Dashboard</p>
      <h1 class="signin-title signin-rise">Sign in</h1>
      <p class="signin-lead signin-rise">Enter the dashboard password to continue.</p>
      <form class="signin-form signin-rise" method="post" action="{SIGN_IN_PATH}" novalidate>
        <input type="hidden" name="next" value="{html.escape(next_path)}" />
        <input type="text" name="username" value="Oscar" autocomplete="username" class="visually-hidden" tabindex="-1" aria-hidden="true" />
        <label class="field-label" for="password">Password</label>
        <div class="password-field">
          <input id="password" name="password" type="password" autocomplete="current-password" autocapitalize="off" spellcheck="false" autofocus{described} />
          <button type="button" class="password-toggle" aria-controls="password" aria-pressed="false" aria-label="Show the password" hidden>Show</button>
        </div>
        {shown}
        <button type="submit" class="primary signin-submit"><span class="btn-label">Sign in</span></button>
      </form>
      <details class="signin-help signin-rise">
        <summary>Forgot the password?</summary>
        <p>Ask whoever runs this Oscar to set a new one. Everyone signed in is then signed out.</p>
      </details>
    """
    return _page("Sign in", card, status=status, headers=headers)


def locked_page() -> HTMLResponse:
    """Shown instead of the dashboard while Oscar is locked: how to set a password."""
    card = f"""
      <p class="eyebrow signin-rise">Locked</p>
      <h1 class="signin-title signin-rise">Set a password first</h1>
      <p class="signin-lead signin-rise">
        Other computers can reach this Oscar, so its dashboard stays locked until it has a password.
      </p>
      <p class="signin-lead signin-rise">
        Whoever runs this Oscar sets one on its server. Then reload this page to sign in.
      </p>
      <p class="signin-foot signin-rise">Phone calls carry on while it is locked.</p>
    """
    return _page("Set a password", card, status=503)


def safe_next(value: str) -> str:
    """Where to go after signing in: a path on this site only, never another site or the sign-in page itself."""
    if (
        not value
        or len(value) > 2000
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or re.search(r"[\x00-\x20\x7f]", value)
        or urlsplit(value).path in (SIGN_IN_PATH, SIGN_OUT_PATH)
    ):
        return "/"
    return value


def _minutes(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


# The gate


class SignIn:
    """The dashboard's sign-in, as the settings ask for it (`Config.dashboard_mode`)."""

    def __init__(self, cfg: Config, store):
        self.mode = cfg.dashboard_mode
        self.public_host = (urlsplit(cfg.public_url).hostname or "").lower() if cfg.public_url else ""
        self._password = cfg.dashboard_password
        self.sessions = Sessions(cfg.dashboard_password, store) if self.mode == "password" else None
        self.failures = FailedSignIns()

    def session_of(self, scope: Scope) -> tuple[str, int] | None:
        """The request's valid session, or None."""
        if self.sessions is None:
            return None
        cookie = cookie_parser(header(scope, b"cookie")).get(COOKIE, "")
        return self.sessions.check(cookie) if cookie else None

    def password_matches(self, given: str) -> bool:
        return bool(self._password) and password_matches(given, self._password)

    def set_session(self, response: Response, scope: Scope) -> None:
        response.set_cookie(
            COOKIE,
            self.sessions.new(),
            max_age=SESSION_SECONDS,
            path="/",
            secure=over_https(scope, self.public_host),
            httponly=True,
            samesite="lax",
        )

    def end_session(self, response: Response, scope: Scope) -> None:
        response.delete_cookie(
            COOKIE, path="/", secure=over_https(scope, self.public_host), httponly=True, samesite="lax"
        )


def sent_from_here(scope: Scope, public_host: str = "") -> bool:
    """Whether a form was sent from Oscar's own page: its Origin, when the browser says one, is
    this address, or PUBLIC_URL's (a proxy in front may send Oscar another Host, such as
    127.0.0.1:7860, as nginx does unless told otherwise)."""
    origin = header(scope, b"origin")
    if not origin:
        return True
    parts = urlsplit(origin)
    if parts.netloc.lower() == header(scope, b"host").strip().lower():
        return True
    return bool(public_host) and parts.scheme == "https" and (parts.hostname or "") == public_host and parts.port is None


class RequireSignIn:
    """Refuses everything without a session (or everything, while Oscar is locked), bar the open paths."""

    def __init__(self, app: ASGIApp, gate: SignIn):
        self.app = app
        self.gate = gate

    def _let_through(self, scope: Scope) -> bool:
        path = scope["path"]
        if is_open(path):
            return True
        if scope["type"] == "http" and (path in (SIGN_IN_PATH, SIGN_OUT_PATH) or path in SIGN_IN_FILES):
            return True
        return self.gate.mode == "password" and self.gate.session_of(scope) is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self._let_through(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        path = scope["path"]
        locked = self.gate.mode == "locked"
        if path.startswith("/api/") or scope["method"] not in ("GET", "HEAD"):
            if locked:
                body, status = {"detail": LOCKED, "locked": True, "sign_in": SIGN_IN_PATH}, 503
            else:
                body, status = {"detail": SIGN_IN_FIRST, "sign_in": SIGN_IN_PATH}, 401
            await JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        if locked:
            await locked_page()(scope, receive, send)
            return
        here = path + ("?" + scope["query_string"].decode("latin-1") if scope.get("query_string") else "")
        target = SIGN_IN_PATH if here == "/" else f"{SIGN_IN_PATH}?next={quote(here, safe='')}"
        await RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})(scope, receive, send)


async def _form(request: Request) -> dict[str, str] | None:
    """The fields of a small form (None when it is too big to be one)."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > FORM_MOST_BYTES:
            return None
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)
    return {name: values[0] for name, values in fields.items()}


def create_sign_in_router(gate: SignIn) -> APIRouter:
    """/login (the page, and signing in) and /logout."""
    router = APIRouter()

    @router.get(SIGN_IN_PATH, include_in_schema=False)
    async def sign_in_form(request: Request, next: str = "", signed_out: str = "") -> Response:
        if gate.mode == "open":
            return RedirectResponse("/", status_code=303)
        if gate.mode == "locked":
            return locked_page()
        if gate.session_of(request.scope):
            return RedirectResponse(safe_next(next), status_code=303)
        return sign_in_page(next_path=safe_next(next), note="You're signed out." if signed_out else "")

    @router.post(SIGN_IN_PATH, include_in_schema=False)
    async def sign_in(request: Request) -> Response:
        if gate.mode == "open":
            return RedirectResponse("/", status_code=303)
        if gate.mode == "locked":
            return locked_page()
        if not sent_from_here(request.scope, gate.public_host):
            return sign_in_page(message=NOT_FROM_HERE, status=403)
        form = await _form(request)
        if form is None:
            return sign_in_page(message=WRONG_PASSWORD, status=413)
        next_path = safe_next(form.get("next", ""))
        who = sign_in_key(client_address(request.scope))
        wait = gate.failures.wait_seconds(who)
        everyone_wait = gate.failures.wait_seconds(EVERYONE, EVERYONE_FAILURES_ALLOWED)
        if wait or everyone_wait:
            whose = "from your connection" if wait else "lately"
            wait = max(wait, everyone_wait)
            return sign_in_page(
                next_path=next_path,
                message=f"Too many wrong passwords {whose}. Try again in {_minutes(wait)}.",
                status=429,
                headers={"Retry-After": str(wait)},
            )
        given = form.get("password", "")
        if not given:
            return sign_in_page(next_path=next_path, message=NO_PASSWORD_TYPED, status=400)
        if not gate.password_matches(given):
            left = gate.failures.failed(who)
            gate.failures.failed(EVERYONE, EVERYONE_FAILURES_ALLOWED)
            logger.warning(f"A wrong dashboard password was typed from {client_address(request.scope)}")
            message = WRONG_PASSWORD
            if left == 0:
                message += f" Sign-in from your connection now waits {_minutes(FAILURE_WINDOW_SECONDS)}."
            elif left <= TRIES_LEFT_SHOWN:
                message += f" {left} more {'try' if left == 1 else 'tries'}, then sign-in waits {_minutes(FAILURE_WINDOW_SECONDS)}."
            return sign_in_page(next_path=next_path, message=message, status=401)
        gate.failures.succeeded(who)
        response = RedirectResponse(next_path, status_code=303, headers={"Cache-Control": "no-store"})
        gate.set_session(response, request.scope)
        return response

    @router.post(SIGN_OUT_PATH, include_in_schema=False)
    async def sign_out(request: Request) -> Response:
        if gate.mode == "open":
            return RedirectResponse("/", status_code=303)
        if not sent_from_here(request.scope, gate.public_host):
            return PlainTextResponse(NOT_FROM_HERE, status_code=403)
        session = gate.session_of(request.scope)
        if session is not None:
            gate.sessions.sign_out(*session)
        response = RedirectResponse(f"{SIGN_IN_PATH}?signed_out=1", status_code=303, headers={"Cache-Control": "no-store"})
        gate.end_session(response, request.scope)
        return response

    return router
