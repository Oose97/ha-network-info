"""Xiaomi MiWiFi router provider.

Talks to the local Luci-style web API of Xiaomi/MiWiFi routers (AX series and
older) to learn, per client, which network path it is on (LAN / 2.4 GHz /
5 GHz / guest) plus signal level and the router-side device name.

Endpoints used:
- ``api/xqsystem/init_info`` (unauthenticated): model + password hash mode
- ``api/xqsystem/login``: nonce-based challenge login, returns the ``stok`` token
- ``api/misystem/devicelist``: all clients the router knows (wired + wireless,
  online and offline)
- ``api/xqnetwork/wifi_connect_devices``: currently associated wireless
  clients with band index and signal
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from ..const import (
    CONNECTION_GUEST,
    CONNECTION_LAN,
    CONNECTION_WIFI,
    CONNECTION_WIFI_24,
    CONNECTION_WIFI_5,
)
from . import (
    RouterAuthError,
    RouterClient,
    RouterConnectionError,
    RouterProvider,
    normalize_mac,
)

_LOGGER = logging.getLogger(__name__)

# Public constant baked into the MiWiFi web UI, part of the login challenge.
_LOGIN_KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"
_TIMEOUT = ClientTimeout(total=15)

_WIFI_INDEX_CONNECTION = {
    1: CONNECTION_WIFI_24,
    2: CONNECTION_WIFI_5,
    3: CONNECTION_GUEST,
}


class XiaomiMiWiFiProvider(RouterProvider):
    """Router provider for Xiaomi MiWiFi firmware."""

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
            scheme = "https" if use_https else "http"
            host = f"{scheme}://{host}"
        self._base = host.rstrip("/")
        self._username = (username or "admin").strip() or "admin"
        self._password = password
        self._session = session
        self._token: str | None = None
        self._new_encrypt = False
        self.model: str | None = None
        # Arbitrary stable device id used in the login nonce.
        self._device_id = "".join(random.choices("0123456789ABCDEF", k=12))

    async def _request_json(
        self, method: str, path: str, data: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Request an API path, following the firmware's HTTP→HTTPS upgrade.

        Newer MiWiFi firmwares (seen on mesh AX models) answer GETs over plain
        HTTP but 301 the login POST to HTTPS with a self-signed certificate.
        aiohttp would follow that redirect as a GET and drop the body, so
        redirects are handled here: upgrade the base to https once and retry
        the same request. The opposite direction is covered too — when the
        default is HTTPS but an older firmware has no TLS listener, one retry
        drops back to HTTP. Certificate verification is off — the router's
        cert is self-signed and the target is a LAN address the user
        configured.
        """
        for attempt in (0, 1):
            url = f"{self._base}{path}"
            try:
                resp = await self._session.request(
                    method,
                    url,
                    data=data,
                    timeout=_TIMEOUT,
                    ssl=False,
                    allow_redirects=False,
                )
            except (ClientError, TimeoutError) as err:
                if attempt == 0 and self._base.startswith("https://"):
                    self._base = "http://" + self._base.split("://", 1)[1]
                    continue
                raise RouterConnectionError(f"Request to router failed: {err}") from err
            try:
                if resp.status in (301, 302, 307, 308):
                    location = resp.headers.get("Location", "")
                    if location.startswith("https://") and attempt == 0:
                        self._base = "https://" + self._base.split("://", 1)[1]
                        continue
                    raise RouterConnectionError(
                        f"Router redirected {path} to unexpected location {location!r}"
                    )
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
            except (ClientError, TimeoutError, ValueError) as err:
                raise RouterConnectionError(f"Request to router failed: {err}") from err
            if not isinstance(payload, dict):
                raise RouterConnectionError("Router returned an unexpected payload")
            return payload
        raise RouterConnectionError("Router kept redirecting")  # pragma: no cover

    async def _get_json(self, path: str) -> dict[str, Any]:
        return await self._request_json("GET", path)

    async def async_login(self) -> None:
        """Authenticate and store the stok token."""
        init = await self._get_json("/cgi-bin/luci/api/xqsystem/init_info")
        self._new_encrypt = str(init.get("newEncryptMode", 0)) == "1"
        self.model = init.get("hardware") or init.get("model") or None

        digest = hashlib.sha256 if self._new_encrypt else hashlib.sha1
        nonce = f"0_{self._device_id}_{int(time.time())}_{random.randint(1000, 9999)}"
        first = digest((self._password + _LOGIN_KEY).encode()).hexdigest()
        password_hash = digest((nonce + first).encode()).hexdigest()

        data = await self._request_json(
            "POST",
            "/cgi-bin/luci/api/xqsystem/login",
            data={
                "username": self._username,
                "password": password_hash,
                "logtype": "2",
                "nonce": nonce,
            },
        )

        if data.get("code") != 0 or not data.get("token"):
            raise RouterAuthError(f"Router rejected login (code {data.get('code')})")
        self._token = data["token"]
        _LOGGER.debug("Logged in to MiWiFi router (model %s)", self.model)

    async def _api(self, path: str) -> dict[str, Any]:
        """Call an authenticated API endpoint, re-logging in once on stale token."""
        if self._token is None:
            await self.async_login()
        data = await self._get_json(f"/cgi-bin/luci/;stok={self._token}/api/{path}")
        if data.get("code") != 0:
            self._token = None
            await self.async_login()
            data = await self._get_json(f"/cgi-bin/luci/;stok={self._token}/api/{path}")
            if data.get("code") != 0:
                raise RouterConnectionError(f"API {path} returned code {data.get('code')}")
        return data

    async def async_get_clients(self) -> dict[str, RouterClient]:
        device_list = await self._api("misystem/devicelist")
        wifi_list = await self._api("xqnetwork/wifi_connect_devices")

        wifi_by_mac: dict[str, dict[str, Any]] = {}
        for item in wifi_list.get("list") or []:
            mac = normalize_mac(item.get("mac"))
            if mac:
                wifi_by_mac[mac] = item

        clients: dict[str, RouterClient] = {}
        for item in device_list.get("list") or []:
            mac = normalize_mac(item.get("mac"))
            if not mac:
                continue
            ips = item.get("ip") or []
            ip = ips[0].get("ip") if ips else None
            online = bool(item.get("online"))
            connection: str | None = None
            signal: int | None = None
            wifi = wifi_by_mac.get(mac)
            if wifi is not None:
                connection = _wifi_connection(wifi)
                signal = _to_int(wifi.get("signal"))
                online = True
            elif online:
                connection = CONNECTION_LAN
            clients[mac] = RouterClient(
                mac=mac,
                ip=ip,
                name=item.get("name") or item.get("oname") or None,
                connection=connection,
                signal=signal,
                online=online,
            )

        # Wireless clients the devicelist somehow missed.
        for mac, wifi in wifi_by_mac.items():
            if mac not in clients:
                clients[mac] = RouterClient(
                    mac=mac,
                    ip=None,
                    name=None,
                    connection=_wifi_connection(wifi),
                    signal=_to_int(wifi.get("signal")),
                    online=True,
                )
        return clients


def _wifi_connection(wifi: dict[str, Any]) -> str:
    idx = _to_int(wifi.get("wifiIndex"))
    return _WIFI_INDEX_CONNECTION.get(idx or 0, CONNECTION_WIFI)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
