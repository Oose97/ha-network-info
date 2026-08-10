"""OpenWrt / LuCI router provider, via the ubus JSON-RPC endpoint.

Covers OpenWrt itself and the many vendor firmwares derived from it — the
Cudy AP1300 among them. Everything goes through `/ubus`: a session login,
then `iwinfo` for the radios and their associated stations.

`iwinfo` is the right source for an access point: `assoclist` returns the
stations actually associated to each radio with their signal, and the radio's
own frequency says which band that is. Wired clients are read from the DHCP
lease list, which only the router of a network has — an access point usually
has none, and reports its wireless stations only.

Which rpcd modules a build ships varies, so stations are looked for in three
places and whichever answers is used: `iwinfo`, LuCI's own `luci-rpc`, and
finally each radio's `hostapd` — an access point cannot serve Wi-Fi without
that last one. A missing object is expected, not an error.

Requires ubus to be reachable over HTTP (`uhttpd-mod-ubus`, standard with
LuCI). Vendor firmwares that strip it cannot be polled this way.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from ..const import CONNECTION_LAN, CONNECTION_WIFI_24, CONNECTION_WIFI_5
from . import (
    RouterAuthError,
    RouterClient,
    RouterConnectionError,
    RouterProvider,
    normalize_mac,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = ClientTimeout(total=20)
_NULL_SESSION = "00000000000000000000000000000000"
_MAC_RE = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", re.IGNORECASE)


class OpenWrtProvider(RouterProvider):
    """Router provider for OpenWrt-based firmware via ubus."""

    def __init__(
        self,
        host: str,
        password: str,
        session: ClientSession,
        username: str = "root",
        use_https: bool = False,
    ) -> None:
        host = host.strip()
        if not host.startswith(("http://", "https://")):
            host = f"{'https' if use_https else 'http'}://{host}"
        self._base = host.rstrip("/")
        self._username = (username or "root").strip() or "root"
        self._password = password
        self._session = session
        self.model: str | None = None
        self._session_id: str | None = None
        self._rpc_id = 0

    async def _rpc(
        self, session_id: str, obj: str, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """One ubus call. Returns the payload, or None when the object is absent."""
        self._rpc_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "call",
            "params": [session_id, obj, method, params or {}],
        }
        try:
            resp = await self._session.post(
                f"{self._base}/ubus", json=body, timeout=_TIMEOUT, ssl=False
            )
            resp.raise_for_status()
            data = json.loads(await resp.text())
        except (ClientError, TimeoutError, json.JSONDecodeError) as err:
            raise RouterConnectionError(f"ubus request failed: {err}") from err

        if not isinstance(data, dict):
            raise RouterConnectionError("ubus returned an unexpected payload")
        if "error" in data:
            message = (data["error"] or {}).get("message", "unknown error")
            raise RouterConnectionError(f"ubus error: {message}")
        result = data.get("result")
        if not isinstance(result, list) or not result:
            return None
        # [status, payload] — status 0 is success, 6 is "access denied".
        status = result[0]
        if status == 6:
            raise RouterAuthError("ubus denied access to this object")
        if status != 0:
            return None
        return result[1] if len(result) > 1 else {}

    async def async_login(self) -> None:
        payload = await self._rpc(
            _NULL_SESSION,
            "session",
            "login",
            {"username": self._username, "password": self._password},
        )
        session_id = (payload or {}).get("ubus_rpc_session")
        if not session_id:
            raise RouterAuthError("Router rejected the credentials")
        self._session_id = session_id
        _LOGGER.debug("Logged in to OpenWrt router via ubus")

    async def async_get_clients(self) -> dict[str, RouterClient]:
        if self._session_id is None:
            await self.async_login()
        try:
            clients = await self._async_collect()
        except RouterAuthError:
            # Sessions expire; one silent re-login before giving up.
            self._session_id = None
            await self.async_login()
            clients = await self._async_collect()
        return clients

    async def _safe_rpc(
        self, obj: str, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """An optional call: a missing object is not a failure of the whole poll.

        Vendor builds ship different subsets of the rpcd modules — Cudy has no
        `iwinfo`, for one — so every source is tried and whatever answers is
        used.
        """
        assert self._session_id is not None
        try:
            return await self._rpc(self._session_id, obj, method, params)
        except RouterAuthError:
            raise
        except RouterConnectionError as err:
            _LOGGER.debug("ubus %s.%s unavailable: %s", obj, method, err)
            return None

    async def _async_collect(self) -> dict[str, RouterClient]:
        assert self._session_id is not None
        clients: dict[str, RouterClient] = {}

        for source in (
            self._async_stations_iwinfo,
            self._async_stations_luci,
            self._async_stations_hostapd,
        ):
            stations = await source()
            if stations:
                clients.update(stations)
                break
        if not clients:
            _LOGGER.debug("No ubus source reported any wireless stations")

        # Leases give the wired clients their IP and hostname — present on a
        # router, absent on a plain access point, which is fine either way.
        leases = await self._safe_rpc("dhcp", "ipv4leases")
        for entry in _iter_leases(leases):
            mac = normalize_mac(entry.get("mac") or entry.get("macaddr"))
            if not mac:
                continue
            ip = entry.get("ip") or entry.get("ipaddr")
            hostname = entry.get("hostname") or entry.get("name")
            existing = clients.get(mac)
            if existing is not None:
                existing.ip = existing.ip or ip
                existing.name = existing.name or hostname
            else:
                clients[mac] = RouterClient(
                    mac=mac,
                    ip=ip,
                    name=hostname,
                    connection=CONNECTION_LAN,
                    signal=None,
                    online=True,
                )
        return clients


    async def _async_stations_iwinfo(self) -> dict[str, RouterClient]:
        """The canonical source, where rpcd-mod-iwinfo is installed."""
        devices = await self._safe_rpc("iwinfo", "devices")
        clients: dict[str, RouterClient] = {}
        for radio in (devices or {}).get("devices") or []:
            info = await self._safe_rpc("iwinfo", "info", {"device": radio})
            band = _band_of(info or {})
            assoc = await self._safe_rpc("iwinfo", "assoclist", {"device": radio})
            for station in (assoc or {}).get("results") or []:
                mac = normalize_mac(station.get("mac"))
                if mac:
                    clients[mac] = _station(mac, band, station.get("signal"))
        return clients

    async def _async_stations_luci(self) -> dict[str, RouterClient]:
        """LuCI's own RPC object, present on builds without iwinfo."""
        payload = await self._safe_rpc("luci-rpc", "getWirelessDevices")
        clients: dict[str, RouterClient] = {}
        for radio in (payload or {}).values():
            if not isinstance(radio, dict):
                continue
            band = _band_of(_find_iwinfo(radio))
            for mac, station in _find_assoclist(radio).items():
                clients[mac] = _station(mac, band, station.get("signal"))
        return clients

    async def _async_stations_hostapd(self) -> dict[str, RouterClient]:
        """Ask each radio's hostapd directly — the last resort, and the most
        widely present, since an access point cannot serve Wi-Fi without it."""
        status = await self._safe_rpc("network.wireless", "status")
        clients: dict[str, RouterClient] = {}
        for radio in (status or {}).values():
            if not isinstance(radio, dict):
                continue
            band = _band_of(_find_iwinfo(radio))
            for interface in radio.get("interfaces") or []:
                ifname = (interface or {}).get("ifname")
                if not ifname:
                    continue
                payload = await self._safe_rpc(f"hostapd.{ifname}", "get_clients")
                for mac, station in ((payload or {}).get("clients") or {}).items():
                    normalized = normalize_mac(mac)
                    if normalized and isinstance(station, dict):
                        clients[normalized] = _station(
                            normalized, band, station.get("signal")
                        )
        return clients


def _station(mac: str, band: str | None, signal: Any) -> RouterClient:
    return RouterClient(
        mac=mac,
        ip=None,
        name=None,
        connection=band,
        signal=_to_int(signal),
        online=True,
    )


def _find_iwinfo(node: Any, depth: int = 0) -> dict[str, Any]:
    """The nearest frequency/channel in a radio's tree, whatever nests it."""
    if depth > 4 or not isinstance(node, dict):
        return {}
    if "frequency" in node or "channel" in node:
        return node
    for value in node.values():
        if isinstance(value, dict):
            found = _find_iwinfo(value, depth + 1)
            if found:
                return found
        elif isinstance(value, list):
            for item in value:
                found = _find_iwinfo(item, depth + 1)
                if found:
                    return found
    return {}


def _find_assoclist(node: Any, depth: int = 0) -> dict[str, dict[str, Any]]:
    """Any MAC-keyed mapping in the tree is an association list."""
    out: dict[str, dict[str, Any]] = {}
    if depth > 4 or not isinstance(node, (dict, list)):
        return out
    values = node.values() if isinstance(node, dict) else node
    if isinstance(node, dict):
        macs = {
            normalize_mac(k): v
            for k, v in node.items()
            if isinstance(v, dict) and _MAC_RE.fullmatch(str(k).strip())
        }
        if macs:
            return {m: v for m, v in macs.items() if m}
    for value in values:
        out.update(_find_assoclist(value, depth + 1))
    return out


def _band_of(info: dict[str, Any]) -> str | None:
    """2.4 or 5 GHz from the radio's frequency, falling back to its channel."""
    frequency = _to_int(info.get("frequency"))
    if frequency:
        return CONNECTION_WIFI_5 if frequency >= 4000 else CONNECTION_WIFI_24
    channel = _to_int(info.get("channel"))
    if channel:
        return CONNECTION_WIFI_24 if channel <= 14 else CONNECTION_WIFI_5
    return None


def _iter_leases(payload: Any) -> list[dict[str, Any]]:
    """ubus dhcp lease payloads differ between releases; accept both shapes."""
    if not isinstance(payload, dict):
        return []
    devices = payload.get("device")
    if isinstance(devices, dict):
        out: list[dict[str, Any]] = []
        for entry in devices.values():
            if isinstance(entry, dict):
                out.extend(
                    lease
                    for lease in entry.get("leases", [])
                    if isinstance(lease, dict)
                )
        return out
    leases = payload.get("leases")
    return [lease for lease in leases or [] if isinstance(lease, dict)]


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
