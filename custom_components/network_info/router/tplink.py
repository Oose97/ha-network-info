"""TP-Link Archer router provider.

Covers the consumer Archer line (C6, C7, A7 and relatives). These log in with
a password only — there is no username field in their web UI — but *how* the
password is sent changed across firmware generations, and a given model can
ship any of them:

1. plain  — base64 of the password
2. rsa    — the password RSA-encrypted with a key the router hands out
3. signed — AES-encrypted payload plus an RSA signature (newest)

Which one applies is worked out *before* logging in, from the key material the
login page hands out unauthenticated, so exactly one attempt is ever spent:
these routers lock the account after a handful of failures, and probing by
trial would burn that budget. The request wrapper differs too — older builds
take form fields, newer ones a JSON envelope — and a 403 identifies that
mismatch without counting as a failed login.

The client list comes from `admin/status?form=client_status`, whose entries
carry a `wire_type` of `wired`, `2.4G` or `5G` — the band, stated outright.

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
    async def _post(
        self, path: str, data: dict[str, Any], as_json: bool = False
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"json": data} if as_json else {"data": data}
        try:
            resp = await self._session.post(
                f"{self._base}{path}",
                headers={"Referer": f"{self._base}/"},
                timeout=_TIMEOUT,
                ssl=False,
                **kwargs,
            )
            resp.raise_for_status()
            text = await resp.text()
        except (ClientError, TimeoutError) as err:
            raise RouterConnectionError(
                f"Request to router failed: {err or type(err).__name__}"
            ) from err
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise RouterConnectionError(
                f"Router returned non-JSON for {path}: {text[:80]!r}"
            ) from err
        return payload if isinstance(payload, dict) else {}

    async def _post_either_envelope(
        self, path: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Post the fields form-encoded, falling back to the JSON envelope.

        Firmware generations differ in the wrapper as well as the payload:
        older builds take `operation=login&…` form fields, newer ones want
        `{"method": "do", "login": {…}}`. A 403 means the wrapper was not
        understood, which is not a failed login and does not count against the
        router's attempt limit — so retrying the other shape is safe. Anything
        that comes back as JSON is an answer, and is returned as-is.
        """
        form = {"operation": "login", **fields}
        try:
            return await self._post(path, form)
        except RouterConnectionError as err:
            if "403" not in str(err):
                raise
            _LOGGER.debug("Form-encoded request refused (403); trying JSON envelope")
        return await self._post(path, {"method": "do", "login": fields}, as_json=True)

    # ── login ────────────────────────────────────────────────────────────
    async def async_login(self) -> None:
        """Work out which login this firmware wants, then attempt it once.

        The router counts failed logins and locks the account out after a
        handful, so trying every variant in turn would burn that budget for
        nothing. The key material the router hands out is readable without
        logging in and says which generation this is, so the choice is made
        first and exactly one attempt follows.
        """
        variant, keys, auth = await self._async_detect_variant()
        _LOGGER.debug("TP-Link login variant detected: %s", variant)

        payload = await self._attempt(variant, keys, auth)
        stok = _stok_of(payload)
        if stok:
            self._stok = stok
            self._variant = variant
            _LOGGER.debug("Logged in to TP-Link router (%s variant)", variant)
            return

        _LOGGER.debug("TP-Link login reply: %s", str(payload)[:200])
        raise RouterAuthError(_login_error(payload, variant))

    async def _async_detect_variant(
        self,
    ) -> tuple[str, tuple[int, int] | None, dict[str, Any]]:
        """Read the login page's key material — no attempt is spent doing so."""
        keys_payload = await self._read_or_empty("keys")
        auth_payload = await self._read_or_empty("auth")
        password_key = _rsa_key(keys_payload)
        sign_key = _rsa_key(auth_payload, field="key")
        sequence = _to_int((auth_payload.get("data") or {}).get("seq"))
        # The public key material is not secret, and its exact shape is what
        # decides how the password must be encoded — worth seeing in full when
        # a firmware refuses a login for reasons it will not name.
        _LOGGER.debug("TP-Link ?form=keys replied: %s", str(keys_payload)[:400])
        _LOGGER.debug("TP-Link ?form=auth replied: %s", str(auth_payload)[:400])
        if password_key:
            _LOGGER.debug(
                "Password key: %d-bit modulus, exponent %d",
                password_key[0].bit_length(),
                password_key[1],
            )
        if sign_key:
            _LOGGER.debug(
                "Sign key: %d-bit modulus, exponent %d, seq %s",
                sign_key[0].bit_length(),
                sign_key[1],
                sequence,
            )

        if sign_key is not None and sequence is not None and _aes_encryptor():
            return "signed", password_key, auth_payload
        if password_key is not None:
            return "rsa", password_key, auth_payload
        return "plain", None, auth_payload

    async def _read_or_empty(self, form: str) -> dict[str, Any]:
        try:
            return await self._post(
                f"/cgi-bin/luci/;stok=/login?form={form}", {"operation": "read"}
            )
        except RouterConnectionError as err:
            _LOGGER.debug("TP-Link ?form=%s unavailable: %s", form, err)
            return {}

    async def _attempt(
        self, variant: str, key: tuple[int, int] | None, auth: dict[str, Any]
    ) -> dict[str, Any]:
        path = "/cgi-bin/luci/;stok=/login?form=login"
        if variant == "plain":
            return await self._post_either_envelope(
                path, {"password": base64.b64encode(self._password.encode()).decode()}
            )
        if variant == "rsa":
            assert key is not None
            return await self._post_either_envelope(
                path, {"password": _rsa_encrypt(self._password.encode(), *key)}
            )

        aes = _aes_encryptor()
        sign_key = _rsa_key(auth, field="key")
        sequence = _to_int((auth.get("data") or {}).get("seq"))
        assert aes is not None and sign_key is not None and sequence is not None

        # A fresh AES-128-CBC key/iv per login, as digit strings — the UI
        # generates them the same way, and the router expects that shape.
        aes_key = _digits(16)
        aes_iv = _digits(16)
        data = aes(aes_key, aes_iv, self._password.encode())
        digest = hashlib.md5(f"{self._username}{self._password}".encode()).hexdigest()
        signature = _rsa_encrypt(
            f"k={aes_key}&i={aes_iv}&h={digest}&s={sequence + len(data)}".encode(),
            *sign_key,
        )
        return await self._post_either_envelope(path, {"data": data, "sign": signature})

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
    """The session token, from either API shape."""
    # Newer JSON API: {"error_code": 0, "stok": "..."}
    if payload.get("stok") and not payload.get("error_code"):
        return str(payload["stok"])
    if payload.get("success"):
        stok = (payload.get("data") or {}).get("stok")
        if stok:
            return str(stok)
    return None


def _login_error(payload: dict[str, Any], variant: str) -> str:
    """Say what the router said, including how many attempts are left.

    These routers lock the account after a fixed number of failures, so the
    remaining budget is the most useful thing to pass on.
    """
    data = payload.get("data") or {}
    remaining = _to_int(data.get("attemptsAllowed"))
    failures = _to_int(data.get("failureCount"))
    code = str(payload.get("errorcode") or payload.get("error_code") or "").lower()

    if "exceed" in code or "attempt" in code or remaining == 0:
        return (
            "Router has locked out logins after too many failed attempts — "
            "wait for it to clear, or reboot the router, before trying again"
        )
    if remaining is not None:
        return (
            f"Router rejected the password ({variant} login) — "
            f"{remaining} attempt(s) left before it locks out"
        )
    if failures is not None:
        return f"Router rejected the password ({variant} login)"
    if code:
        return f"Router refused the {variant} login: {code}"
    return f"Router refused the {variant} login"


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
