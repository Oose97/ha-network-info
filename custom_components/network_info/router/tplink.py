"""TP-Link Archer router provider.

Covers the consumer Archer line (C6, C7, A7 and relatives). These log in with
a password only — there is no username field in their web UI — but *how* the
password is sent changed across firmware generations, and a given model can
ship any of them. All three are attempted in turn, cheapest first:

1. plain  — base64 of the password
2. rsa    — the password RSA-encrypted with a key the router hands out
3. signed — AES-encrypted payload plus an RSA signature (newest)

Whichever succeeds is remembered for subsequent logins. The client list then
comes from `admin/status?form=client_status`, whose entries carry a
`wire_type` of `wired`, `2.4G` or `5G` — the band, stated outright.

The signed variant needs AES, taken from `cryptography`, which Home Assistant
itself depends on; if it were unavailable that one variant is skipped rather
than breaking the provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from ..const import (
    CONNECTION_GUEST,
    CONNECTION_LAN,
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

_TIMEOUT = ClientTimeout(total=20)
_WIRE_TYPES = {
    "wired": CONNECTION_LAN,
    "2.4g": CONNECTION_WIFI_24,
    "2.4ghz": CONNECTION_WIFI_24,
    "5g": CONNECTION_WIFI_5,
    "5ghz": CONNECTION_WIFI_5,
    "5g1": CONNECTION_WIFI_5,
    "5g2": CONNECTION_WIFI_5,
}


class TPLinkProvider(RouterProvider):
    """Router provider for TP-Link Archer web UIs."""

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
        # The UI has no username field; "admin" is what the firmware assumes
        # internally, and it is only used in the signed variant's hash.
        self._username = (username or "admin").strip() or "admin"
        self._password = password
        self._session = session
        self.model: str | None = None
        self._stok: str | None = None
        self._variant: str | None = None

    # ── transport ────────────────────────────────────────────────────────
    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            resp = await self._session.post(
                f"{self._base}{path}",
                data=data,
                headers={"Referer": f"{self._base}/"},
                timeout=_TIMEOUT,
                ssl=False,
            )
            resp.raise_for_status()
            text = await resp.text()
        except (ClientError, TimeoutError) as err:
            raise RouterConnectionError(f"Request to router failed: {err}") from err
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise RouterConnectionError(
                f"Router returned non-JSON for {path}: {text[:80]!r}"
            ) from err
        return payload if isinstance(payload, dict) else {}

    # ── login ────────────────────────────────────────────────────────────
    async def async_login(self) -> None:
        variants = [
            ("plain", self._login_plain),
            ("rsa", self._login_rsa),
            ("signed", self._login_signed),
        ]
        if self._variant:  # a known-good variant goes first
            variants.sort(key=lambda v: v[0] != self._variant)

        errors: list[str] = []
        for name, attempt in variants:
            try:
                stok = await attempt()
            except RouterConnectionError as err:
                errors.append(f"{name}: {err}")
                continue
            except Exception as err:  # noqa: BLE001 - try the next variant
                errors.append(f"{name}: {err}")
                continue
            if stok:
                self._stok = stok
                self._variant = name
                _LOGGER.debug("Logged in to TP-Link router (%s variant)", name)
                return
            errors.append(f"{name}: rejected")

        raise RouterAuthError(
            "No supported login variant succeeded — " + "; ".join(errors)
        )

    async def _login_plain(self) -> str | None:
        payload = await self._post(
            "/cgi-bin/luci/;stok=/login?form=login",
            {
                "operation": "login",
                "password": base64.b64encode(self._password.encode()).decode(),
            },
        )
        return _stok_of(payload)

    async def _login_rsa(self) -> str | None:
        keys = await self._post(
            "/cgi-bin/luci/;stok=/login?form=keys", {"operation": "read"}
        )
        key = _rsa_key(keys)
        if key is None:
            return None
        encrypted = _rsa_encrypt(self._password.encode(), *key)
        payload = await self._post(
            "/cgi-bin/luci/;stok=/login?form=login",
            {"operation": "login", "password": encrypted},
        )
        return _stok_of(payload)

    async def _login_signed(self) -> str | None:
        aes = _aes_encryptor()
        if aes is None:
            _LOGGER.debug("cryptography unavailable — skipping the signed variant")
            return None

        keys = await self._post(
            "/cgi-bin/luci/;stok=/login?form=keys", {"operation": "read"}
        )
        auth = await self._post(
            "/cgi-bin/luci/;stok=/login?form=auth", {"operation": "read"}
        )
        password_key = _rsa_key(keys)
        sign_key = _rsa_key(auth, field="key")
        sequence = _to_int((auth.get("data") or {}).get("seq"))
        if password_key is None or sign_key is None or sequence is None:
            return None

        # A fresh AES-128-CBC key/iv per login, as digit strings — the UI
        # generates them the same way, and the router expects that shape.
        aes_key = _digits(16)
        aes_iv = _digits(16)
        data = aes(aes_key, aes_iv, self._password.encode())

        digest = hashlib.md5(
            f"{self._username}{self._password}".encode()
        ).hexdigest()
        signature = _rsa_encrypt(
            f"k={aes_key}&i={aes_iv}&h={digest}&s={sequence + len(data)}".encode(),
            *sign_key,
        )
        payload = await self._post(
            "/cgi-bin/luci/;stok=/login?form=login",
            {"operation": "login", "data": data, "sign": signature},
        )
        return _stok_of(payload)

    # ── clients ──────────────────────────────────────────────────────────
    async def async_get_clients(self) -> dict[str, RouterClient]:
        if self._stok is None:
            await self.async_login()
        clients = await self._async_read_clients()
        if clients is None:
            # An expired stok answers with an error rather than data.
            self._stok = None
            await self.async_login()
            clients = await self._async_read_clients()
        if clients is None:
            raise RouterConnectionError("Router did not return a client list")
        return clients

    async def _async_read_clients(self) -> dict[str, RouterClient] | None:
        for path, body in (
            (f"/cgi-bin/luci/;stok={self._stok}/admin/status?form=client_status",
             {"operation": "read"}),
            (f"/cgi-bin/luci/;stok={self._stok}/admin/wireless?form=statistics",
             {"operation": "load"}),
        ):
            try:
                payload = await self._post(path, body)
            except RouterConnectionError:
                continue
            if not payload.get("success", True):
                continue
            clients = _parse_clients(payload.get("data"))
            if clients:
                return clients
        return None


def _parse_clients(data: Any) -> dict[str, RouterClient]:
    """Read the client lists, whichever grouping this firmware uses."""
    clients: dict[str, RouterClient] = {}
    groups: list[tuple[str, list[Any]]] = []
    if isinstance(data, dict):
        groups = [(k, v) for k, v in data.items() if isinstance(v, list)]
    elif isinstance(data, list):
        groups = [("", data)]

    for group_name, items in groups:
        default = CONNECTION_GUEST if "guest" in group_name.lower() else None
        if "wired" in group_name.lower():
            default = CONNECTION_LAN
        for item in items:
            if not isinstance(item, dict):
                continue
            mac = normalize_mac(item.get("macaddr") or item.get("mac"))
            if not mac:
                continue
            wire = str(item.get("wire_type") or item.get("type") or "").lower()
            clients[mac] = RouterClient(
                mac=mac,
                ip=(item.get("ipaddr") or item.get("ip") or "").strip() or None,
                name=(item.get("hostname") or item.get("name") or "").strip() or None,
                connection=_WIRE_TYPES.get(wire, default),
                signal=_to_int(item.get("rssi") or item.get("signal")),
                online=True,
            )
    return clients


def _stok_of(payload: dict[str, Any]) -> str | None:
    if not payload.get("success"):
        return None
    stok = (payload.get("data") or {}).get("stok")
    return str(stok) if stok else None


def _rsa_key(payload: dict[str, Any], field: str = "password") -> tuple[int, int] | None:
    """The (n, e) pair the router publishes, as hex strings."""
    values = (payload.get("data") or {}).get(field)
    if not isinstance(values, list) or len(values) < 2:
        return None
    try:
        return int(str(values[0]), 16), int(str(values[1]), 16)
    except ValueError:
        return None


def _rsa_encrypt(data: bytes, n: int, e: int) -> str:
    """Textbook RSA over zero-padded blocks, hex-encoded — what the UI does."""
    key_len = (n.bit_length() + 7) // 8
    out: list[str] = []
    for start in range(0, len(data), key_len):
        block = data[start : start + key_len]
        cipher = pow(int.from_bytes(block, "big"), e, n)
        out.append(cipher.to_bytes(key_len, "big").hex())
    return "".join(out)


def _digits(length: int) -> str:
    return "".join(str(b % 10) for b in os.urandom(length))


def _aes_encryptor():
    """AES-128-CBC with PKCS#7, or None when cryptography is unavailable."""
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:  # pragma: no cover - present in every HA install
        return None

    def encrypt(key: str, iv: str, data: bytes) -> str:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode()))
        encryptor = cipher.encryptor()
        return base64.b64encode(
            encryptor.update(padded) + encryptor.finalize()
        ).decode()

    return encrypt


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
