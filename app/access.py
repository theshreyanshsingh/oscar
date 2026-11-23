"""Lets in only people who signed in through Cloudflare Access, when it guards the dashboard.

An optional extra layer on top of the dashboard's own password (app/signin.py):
when it is set up, a visitor needs both. On a server, put Cloudflare Access in
front of its web address (README.md, "Protect the dashboard") and set
CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD in .env. Cloudflare then asks each
visitor to sign in, and adds to every request it lets through a token it has
signed: the Cf-Access-Jwt-Assertion header.

Oscar checks that token on every request: signed with one of Cloudflare's keys
for the team, made for this application (its audience tag), and not expired.
So someone who reaches Oscar without passing Cloudflare (straight at the
server's address and port) gets nothing, and the firewall can stay as it is.

Left open, because they cannot sign in: /healthz (it says only that Oscar is up
and how many calls it is on, for deploy.sh and scripts/autostart.py) and
Twilio's webhooks under /twilio/ (each checked by Twilio's own signature, or
by a one-time token for the call's audio).
"""

import asyncio
import time

import httpx
import jwt
from loguru import logger
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

HEADER = b"cf-access-jwt-assertion"
OPEN_PATHS = ("/healthz",)
OPEN_PREFIXES = ("/twilio/",)
# Cloudflare changes its signing keys every few weeks, so a token signed with a
# key not seen yet makes Oscar fetch the keys again: at most this often, so
# made-up tokens cannot have it asking all the time.
REFETCH_AFTER_SECONDS = 60
# Clocks differ a little between Cloudflare and this server.
LEEWAY_SECONDS = 60
CERTS_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

REFUSED = (
    "This Oscar only opens through Cloudflare Access. Go to its web address "
    "(not the server's own address) and sign in there."
)
UNAVAILABLE = (
    "Oscar could not check your Cloudflare Access sign-in just now (it could not get "
    "Cloudflare's keys). Try again in a moment."
)


class AccessUnavailable(Exception):
    """Cloudflare's signing keys could not be had, so no token can be checked."""


def is_open(path: str) -> bool:
    """Whether a path is let through without a sign-in."""
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


class CloudflareAccess:
    """Checks the tokens Cloudflare Access adds to the requests it lets through."""

    def __init__(self, team_domain: str, audience: str, client: httpx.AsyncClient | None = None):
        self.team_domain = team_domain
        self.audience = audience
        self._issuer = f"https://{team_domain}"
        self._certs_url = f"https://{team_domain}/cdn-cgi/access/certs"
        self._client = client or httpx.AsyncClient(timeout=CERTS_TIMEOUT)
        self._keys: dict[str, object] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def signed_in(self, token: str) -> str | None:
        """Who a valid token was given to (their email), or None when it is not valid.

        Raises AccessUnavailable when Cloudflare's keys could not be fetched.
        """
        try:
            key_id = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return None
        key = await self._key(key_id)
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError:
            return None
        return str(claims.get("email") or claims.get("sub") or "a signed-in visitor")

    async def _key(self, key_id) -> object | None:
        if not isinstance(key_id, str) or not key_id:
            return None
        if key_id in self._keys:
            return self._keys[key_id]
        async with self._lock:
            if key_id not in self._keys and self._may_fetch():
                await self._fetch()
        return self._keys.get(key_id)

    def _may_fetch(self) -> bool:
        return self._fetched_at is None or time.monotonic() - self._fetched_at >= REFETCH_AFTER_SECONDS

    async def _fetch(self) -> None:
        try:
            response = await self._client.get(self._certs_url)
            response.raise_for_status()
            listed = response.json()["keys"]
            if not isinstance(listed, list):
                raise TypeError("no list of keys")
        except (httpx.HTTPError, ValueError, LookupError, TypeError) as exc:
            # Not marked as fetched: the next request tries again.
            raise AccessUnavailable(
                f"Could not get Cloudflare Access's signing keys from {self._certs_url} ({exc})."
            ) from None
        keys = {}
        for listed_key in listed:
            try:
                keys[listed_key["kid"]] = jwt.PyJWK(listed_key, algorithm="RS256").key
            except (jwt.PyJWTError, LookupError, TypeError, ValueError):
                continue  # not a key Oscar can use
        self._keys = keys  # a key Cloudflare no longer lists stops working
        self._fetched_at = time.monotonic()


class RequireAccess:
    """Refuses every request (and WebSocket) that Cloudflare Access did not let through, bar the open paths."""

    def __init__(self, app: ASGIApp, access: CloudflareAccess):
        self.app = app
        self.access = access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or is_open(scope["path"]):
            await self.app(scope, receive, send)
            return
        token = ""
        for name, value in scope["headers"]:
            if name == HEADER:
                token = value.decode("latin-1")
        status = 403
        if token:
            try:
                if await self.access.signed_in(token):
                    await self.app(scope, receive, send)
                    return
            except AccessUnavailable as exc:
                logger.warning(str(exc))
                status = 503
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
        else:
            await PlainTextResponse(UNAVAILABLE if status == 503 else REFUSED, status_code=status)(
                scope, receive, send
            )
