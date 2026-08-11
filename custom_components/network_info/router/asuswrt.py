"""ASUSWRT router provider.

Talks to the local web API of ASUS routers running ASUSWRT (RT series, e.g.
the RT-AC65P) and Merlin builds. Login posts a base64 `user:password` to
`login.cgi` for a session token; `appGet.cgi?hook=get_clientlist()` then
returns every client the router knows.

That one call is unusually complete: each client carries its band directly
(`isWL`: wired, 2.4 GHz, or one of the 5 GHz radios), its RSSI, and whether it
is currently online — so no second endpoint or heuristics are needed.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from ..const import (
    CONNECTION_LAN,
    CONNECTION_WIFI_24,
    CONNECTION_WIFI_5,
    CONNECTION_WIFI_6,
)
from . import (
    RouterAuthError,
    RouterClient,
    RouterConnectionError,
    RouterProvider,
    normalize_mac,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = ClientTimeout(total=20)
# ASUSWRT rejects requests that do not look like they came from its own UI.
_UA = "Mozilla/5.0 (compatible; HomeAssistant Network Info)"
_TOKEN_RE = re.compile(r"asus_token\s*[=:]\s*['\"]?([A-Za-z0-9._~+/-]{8,})")
_ERROR_RE = re.compile(r"error_status\s*[=:]\s*['\"]?(\d+)")

# isWL: 0 wired, 1 the 2.4 GHz radio, 2 and 3 the 5 GHz radios, 4 the
# 6 GHz radio on Wi-Fi 6E/7 models.
_BAND_BY_ISWL = {
    "0": CONNECTION_LAN,
    "1": CONNECTION_WIFI_24,
    "2": CONNECTION_WIFI_5,
    "3": CONNECTION_WIFI_5,
    "4": CONNECTION_WIFI_6,
}


class AsusWrtProvider(RouterProvider):
    """Router provider for ASUSWRT."""

    def __init__(
        self,
        host: str,
        password: str,
        session: ClientSession,
        username: str = "admin",
        use_https: bool = False,
    ) -> None:
        host = host.strip()
        if not host.startswith(("http://", "https://")):
            host = f"{'https' if use_https else 'http'}://{host}"
        self._base = host.rstrip("/")
        self._username = (username or "admin").strip() or "admin"
        self._password = password
        self._session = session
        self.model: str | None = None
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": _UA, "Referer": f"{self._base}/"}
        if self._token:
            headers["Cookie"] = f"asus_token={self._token}"
        return headers

    async def async_login(self) -> None:
        auth = base64.b64encode(
            f"{self._username}:{self._password}".encode()
        ).decode()
        try:
            resp = await self._session.post(
                f"{self._base}/login.cgi",
                data={"login_authorization": auth},
                headers=self._headers(),
                timeout=_TIMEOUT,
                ssl=False,
            )
            body = await resp.text()
        except (ClientError, TimeoutError) as err:
            raise RouterConnectionError(
                f"Login request failed: {err or type(err).__name__}"
            ) from err

        # Where the token lands depends on the firmware: a JSON body, a
        # Set-Cookie header, or embedded in an HTML redirect page. The cookie
        # is read from the response directly rather than from the shared
        # session jar, which discards cookies set by a bare-IP host.
        token = _token_from_json(body)
        if not token:
            morsel = resp.cookies.get("asus_token")
            token = morsel.value if morsel else None
        if not token:
            token = _token_from_body(body)

        if not token:
            reason = _login_error(body)
            _LOGGER.debug(
                "ASUSWRT login gave no token (HTTP %s): %s",
                resp.status,
                body[:200].replace("\n", " "),
            )
            raise RouterAuthError(reason)
        self._token = token
        _LOGGER.debug("Logged in to ASUSWRT router")

    async def async_get_clients(self) -> dict[str, RouterClient]:
        if self._token is None:
            await self.async_login()
        payload = await self._hook("get_clientlist()")
        if payload is None:
            # A stale token yields a login redirect rather than JSON.
            self._token = None
            await self.async_login()
            payload = await self._hook("get_clientlist()")
        if payload is None:
            raise RouterConnectionError("Router did not return a client list")

        raw = payload.get("get_clientlist") or {}
        clients: dict[str, RouterClient] = {}
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue  # "maclist" and friends are plain lists
            mac = normalize_mac(item.get("mac") or key)
            if not mac:
                continue
            is_wl = str(item.get("isWL", "")).strip()
            clients[mac] = RouterClient(
                mac=mac,
                ip=(item.get("ip") or "").strip() or None,
                name=(item.get("nickName") or item.get("name") or "").strip() or None,
                connection=_BAND_BY_ISWL.get(is_wl),
                signal=_to_int(item.get("rssi")),
                online=str(item.get("isOnline", "")).strip() == "1",
            )
        return clients

    async def _hook(self, hook: str) -> dict[str, Any] | None:
        try:
            resp = await self._session.get(
                f"{self._base}/appGet.cgi",
                params={"hook": hook},
                headers=self._headers(),
                timeout=_TIMEOUT,
                ssl=False,
            )
            resp.raise_for_status()
            text = await resp.text()
        except (ClientError, TimeoutError) as err:
            raise RouterConnectionError(
                f"Request to router failed: {err or type(err).__name__}"
            ) from err
        return _loads(text)


def _token_from_json(body: str) -> str | None:
    data = _loads(body)
    token = data.get("asus_token") if data else None
    return str(token) if token else None


def _token_from_body(body: str) -> str | None:
    """Some firmware only echoes the token inside the redirect page."""
    match = _TOKEN_RE.search(body)
    return match.group(1) if match else None


def _login_error(body: str) -> str:
    """Turn ASUSWRT's error_status into something worth reading."""
    match = _ERROR_RE.search(body)
    status = match.group(1) if match else None
    return {
        "2": "Router rejected the username",
        "3": "Router rejected the password",
        "7": (
            "Router has locked out logins after too many failed attempts — "
            "wait for the lockout to expire, then try again"
        ),
        "8": "Router refused a login from this address",
    }.get(status or "", "Router did not return a session token")


def _loads(text: str) -> dict[str, Any] | None:
    """ASUSWRT emits not-quite-JSON at times (unquoted keys, trailing commas)."""
    text = text.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
