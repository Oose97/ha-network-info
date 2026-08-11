"""Technicolor Homeware router provider.

Talks to the local web UI of Technicolor Homeware gateways (DGA/TG "ac" series,
e.g. the Telia X1 / DGA0122) to list the network's clients. Authentication is
the gateway's SRP-6 handshake (see :mod:`.srp6`); the client list comes from
``modals/device-modal.lp``.

The device modal reliably carries hostname, IPv4 and MAC. Connection path
(LAN / 2.4 GHz / 5 GHz) and signal are parsed best-effort from whatever the
build exposes and left unknown when absent — Homeware's modal layout varies by
firmware, and this provider has not been validated against every build.

Reference protocol: https://github.com/shaiu/technicolor (GPL). This is an
independent, dependency-free reimplementation (aiohttp + stdlib HTML parsing).
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from ..const import (
    CONNECTION_GUEST,
    CONNECTION_LAN,
    CONNECTION_WIFI,
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
from .srp6 import SRP6Client

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = ClientTimeout(total=20)
_CSRF_RE = re.compile(
    r'name=["\']CSRFtoken["\'][^>]*content=["\']([^"\']+)["\']'
    r'|content=["\']([^"\']+)["\'][^>]*name=["\']CSRFtoken["\']',
    re.IGNORECASE,
)
_MAC_RE = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
# Radio markers as they appear in wireless-modal headings and interface names.
_BAND_RE = re.compile(
    r"\b(?:6\s?GHz|5\s?GHz|2[.,]4\s?GHz|guest|wl[0-9])\b", re.IGNORECASE
)
# "-58 dBm", "58 dBm", "Signal: 75%".
_SIGNAL_RE = re.compile(r"(-?\d{1,3})\s*(?:dBm|%)", re.IGNORECASE)


def _classify(text: str) -> str | None:
    """Map an interface/‘connected via’ description to a connection path."""
    t = text.lower()
    if not t.strip():
        return None
    if "guest" in t:
        return CONNECTION_GUEST
    if "6g" in t or "6 g" in t:
        return CONNECTION_WIFI_6
    if "5g" in t or "5 g" in t or "wl1" in t or "wl2" in t:
        return CONNECTION_WIFI_5
    if "2.4" in t or "2,4" in t or "2g" in t or "wl0" in t:
        return CONNECTION_WIFI_24
    if any(k in t for k in ("wireless", "wifi", "wi-fi", "wlan", "ssid")):
        return CONNECTION_WIFI
    if any(k in t for k in ("ethernet", "eth", "wired", "lan", "cable")):
        return CONNECTION_LAN
    return None


class _TableParser(HTMLParser):
    """Extract every <table>'s rows as lists of cell text (stdlib only)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._is_header = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._is_header = tag == "th"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            self._row.append(text)  # type: ignore[union-attr]
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


class TechnicolorProvider(RouterProvider):
    """Router provider for Technicolor Homeware gateways."""

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
        self.model: str | None = None
        self._authenticated = False
        # Cookies are tracked by hand: aiohttp's shared cookie jar runs with
        # unsafe=False and so silently drops cookies set by a bare-IP host
        # (RFC 6265), which is exactly what the gateway is. Without the session
        # cookie the second /authenticate POST is answered 403, so the
        # Set-Cookie from each response is captured and replayed as a Cookie
        # header instead of relying on the jar.
        self._cookies: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        # Homeware gates its AJAX endpoints on the XHR header (and a same-origin
        # Referer); the browser UI sends both, a bare request gets 403.
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._base}/",
        }
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        return headers

    def _store_cookies(self, resp: Any) -> None:
        for key, morsel in resp.cookies.items():
            self._cookies[key] = morsel.value

    async def _get(self, path: str) -> str:
        try:
            resp = await self._session.get(
                f"{self._base}{path}",
                timeout=_TIMEOUT,
                ssl=False,
                headers=self._headers(),
            )
            self._store_cookies(resp)
            resp.raise_for_status()
            return await resp.text()
        except (ClientError, TimeoutError) as err:
            raise RouterConnectionError(
                f"Request to gateway failed: {err or type(err).__name__}"
            ) from err

    async def _post(self, path: str, data: dict[str, str]) -> str:
        try:
            resp = await self._session.post(
                f"{self._base}{path}",
                data=data,
                timeout=_TIMEOUT,
                ssl=False,
                headers=self._headers(),
            )
            self._store_cookies(resp)
            resp.raise_for_status()
            return await resp.text()
        except (ClientError, TimeoutError) as err:
            raise RouterConnectionError(
                f"Request to gateway failed: {err or type(err).__name__}"
            ) from err

    async def async_login(self) -> None:
        """Run the SRP-6 handshake against the gateway's web UI."""
        home = await self._get("/")
        match = _CSRF_RE.search(home)
        if not match:
            raise RouterConnectionError(
                "No CSRF token on the gateway home page — not a Homeware UI?"
            )
        token = match.group(1) or match.group(2)

        srp = SRP6Client(self._username, self._password)
        step1 = await self._post(
            "/authenticate",
            {"CSRFtoken": token, "I": srp.username, "A": srp.a_bytes.hex()},
        )
        challenge = _parse_json(step1)
        if "s" not in challenge or "B" not in challenge:
            raise RouterAuthError(f"Gateway refused the handshake: {challenge}")

        proof = srp.process_challenge(
            bytes.fromhex(challenge["s"]), bytes.fromhex(challenge["B"])
        )
        if proof is None:
            raise RouterAuthError("SRP safety check failed")

        step2 = await self._post(
            "/authenticate", {"CSRFtoken": token, "M": proof.hex()}
        )
        result = _parse_json(step2)
        if "error" in result or "M" not in result:
            raise RouterAuthError(f"Gateway rejected the credentials: {result}")
        if not srp.verify_session(bytes.fromhex(result["M"])):
            raise RouterAuthError("Gateway session proof did not verify")
        self._authenticated = True

    async def async_get_clients(self) -> dict[str, RouterClient]:
        if not self._authenticated:
            await self.async_login()
        content = await self._get("/modals/device-modal.lp")
        clients = _parse_device_modal(content)
        if not clients:
            # Some builds serve an empty modal until this alternate is hit.
            content = await self._get("/modals/ipv6devices-modal.lp")
            clients = _parse_device_modal(content)

        # The device modal knows who exists, not how they are attached — many
        # builds carry no interface column at all. The wireless modal lists the
        # currently associated stations per radio, which is where the band and
        # signal come from; anything online and absent from it is reached over
        # a wired port (from the gateway's point of view — a device behind a
        # downstream access point or switch is wired as far as it can tell).
        wireless = await self._async_wireless_stations()
        # A radio-configuration page carries the access points' own BSSIDs, not
        # the clients'. Those look exactly like a station list to a MAC scan,
        # and trusting one would mark every real device wired. If nothing in it
        # matches a known device, it is not a station list — drop it and leave
        # the paths unknown rather than claim something false.
        if wireless and not (wireless.keys() & clients.keys()):
            _LOGGER.debug(
                "Wireless page listed %d MACs, none of them known devices — "
                "treating it as radio config, not a station list",
                len(wireless),
            )
            wireless = {}

        for mac, client in clients.items():
            station = wireless.get(mac)
            if station is not None:
                band, signal = station
                if band:
                    client.connection = band
                client.signal = signal
                client.online = True
            elif client.online and client.connection is None:
                client.connection = CONNECTION_LAN if wireless else None
        return clients

    async def _async_wireless_stations(self) -> dict[str, tuple[str | None, int | None]]:
        """Associated wireless stations, keyed by MAC. Empty when unavailable."""
        for path in ("/modals/wireless-modal.lp", "/modals/wirelessstats-modal.lp"):
            try:
                content = await self._get(path)
            except RouterConnectionError:
                continue
            stations = _parse_wireless_modal(content)
            if stations:
                _LOGGER.debug("Found %d wireless stations via %s", len(stations), path)
                return stations
        _LOGGER.debug("No wireless station list available; paths stay unknown")
        return {}


def _parse_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as err:
        raise RouterConnectionError(
            f"Gateway returned non-JSON on /authenticate: {text[:80]!r}"
        ) from err
    return data if isinstance(data, dict) else {}


def _parse_device_modal(content: str) -> dict[str, RouterClient]:
    """Parse the device modal into clients keyed by MAC.

    Handles the table layout first (header-mapped columns), then falls back to
    scanning for MAC addresses with their surrounding context — so a build this
    provider has never seen still yields MAC/IP/hostname even if the path can't
    be read.
    """
    clients: dict[str, RouterClient] = {}

    parser = _TableParser()
    try:
        parser.feed(content)
    except Exception:  # malformed HTML — fall through to the regex scan
        parser.rows = []

    for table_clients in (_from_table(parser.rows),):
        for mac, client in table_clients.items():
            clients[mac] = client

    if not clients:
        clients = _from_scan(content)
    return clients


def _from_table(rows: list[list[str]]) -> dict[str, RouterClient]:
    if not rows:
        return {}
    header = [c.lower() for c in rows[0]]

    def find(*names: str) -> int | None:
        for i, cell in enumerate(header):
            if any(n in cell for n in names):
                return i
        return None

    def find_all(*names: str) -> list[int]:
        return [
            i for i, cell in enumerate(header) if any(n in cell for n in names)
        ]

    mac_i = find("mac")
    if mac_i is None:
        return {}
    name_i = find("hostname", "name", "device")
    ip_i = find("ipv4", "ip address", "ip")
    # The interface/band lives under a different header per firmware. Every
    # plausible column is collected and their text classified together, so a
    # build that splits it across e.g. "type" (Wi-Fi/Ethernet) and "port"
    # (2.4GHz/LAN1) still resolves — seen on the TG789vac, whose device modal
    # columns are status/hostname/ip/mac/type/port.
    path_is = find_all(
        "type", "port", "interface", "connected via", "access point",
        "link", "radio", "band", "medium", "connection",
    )
    state_i = find("state", "status", "active", "online")
    _LOGGER.debug(
        "Device modal headers %s (mac=%s ip=%s name=%s path=%s state=%s)",
        header, mac_i, ip_i, name_i, path_is, state_i,
    )
    logged_sample = False

    clients: dict[str, RouterClient] = {}
    for row in rows[1:]:
        if len(row) <= mac_i:
            continue
        mac = normalize_mac(_first_mac(row[mac_i]))
        if not mac:
            continue
        ip = _first_ip(row[ip_i]) if ip_i is not None and ip_i < len(row) else None
        name = row[name_i] if name_i is not None and name_i < len(row) else None
        path_text = " ".join(row[i] for i in path_is if i < len(row))
        conn = _classify(path_text) if path_text else None
        if not logged_sample:
            logged_sample = True
            _LOGGER.debug(
                "Device modal first row: cells=%s -> path_text=%r -> %s",
                row, path_text, conn,
            )
        # The modal lists every device the gateway has ever seen. An explicit
        # state column decides when present; otherwise the absence of a current
        # lease (no IP) is what distinguishes a remembered device from a
        # connected one.
        if state_i is not None and state_i < len(row):
            online = _state_is_online(row[state_i])
            if online is None:
                online = bool(ip)
        else:
            online = bool(ip)
        clients[mac] = RouterClient(
            mac=mac,
            ip=ip,
            name=name or None,
            connection=conn,
            signal=None,
            online=online,
        )
    return clients


def _state_is_online(text: str) -> bool | None:
    """Read a state/status cell; None when it says nothing useful."""
    t = text.strip().lower()
    if not t:
        return None
    if any(k in t for k in ("disconnect", "inactive", "offline", "not connected")):
        return False
    if any(k in t for k in ("connected", "active", "online", "yes", "true")):
        return True
    return None


def _from_scan(content: str) -> dict[str, RouterClient]:
    """Last-resort: every MAC with the IP nearest it and any path hint around."""
    clients: dict[str, RouterClient] = {}
    for m in _MAC_RE.finditer(content):
        mac = normalize_mac(m.group(1))
        if not mac or mac in clients:
            continue
        window = content[max(0, m.start() - 400) : m.end() + 400]
        ip = _first_ip(window)
        clients[mac] = RouterClient(
            mac=mac,
            ip=ip,
            name=None,
            connection=_classify(window),
            signal=None,
            online=bool(ip),
        )
    return clients


def _parse_wireless_modal(content: str) -> dict[str, tuple[str | None, int | None]]:
    """Associated stations from the wireless modal, keyed by MAC.

    Layout varies far more than the device modal, so rather than assuming
    columns this walks each MAC and reads the nearest preceding radio heading
    for the band plus any signal figure alongside it.
    """
    stations: dict[str, tuple[str | None, int | None]] = {}
    for m in _MAC_RE.finditer(content):
        mac = normalize_mac(m.group(1))
        if not mac or mac in stations:
            continue
        before = content[max(0, m.start() - 2000) : m.start()]
        after = content[m.end() : m.end() + 300]
        stations[mac] = (_band_from_context(before, after), _signal_from(after))
    return stations


def _band_from_context(before: str, after: str) -> str | None:
    """Nearest preceding radio marker wins; the trailing context is a fallback."""
    for text, reverse in ((before, True), (after, False)):
        hits = [
            (m.start(), m.group(0).lower())
            for m in _BAND_RE.finditer(text)
        ]
        if not hits:
            continue
        _, marker = hits[-1] if reverse else hits[0]
        band = _classify(marker)
        if band:
            return band
    return None


def _signal_from(text: str) -> int | None:
    m = _SIGNAL_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _first_mac(text: str) -> str | None:
    m = _MAC_RE.search(text)
    return m.group(1) if m else None


def _first_ip(text: str) -> str | None:
    m = _IPV4_RE.search(text)
    return m.group(1) if m else None
