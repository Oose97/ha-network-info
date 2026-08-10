"""TP-Link Archer router provider.

Covers the consumer Archer line (C6, C7, A7 and relatives). These log in with
a password only — there is no username field in their web UI — but *how* the
password is sent changed across firmware generations, and a given model can
ship any of them:

1. plain  — base64 of the password
2. rsa    — the password RSA-encrypted with a key the router hands out
3. signed — an AES-encrypted payload plus an RSA signature (newest)

Which one applies is worked out *before* logging in, from the key material the
login page hands out unauthenticated, so exactly one attempt is ever spent:
these routers lock the account after a handful of failures, and probing by
trial would burn that budget. A 403 identifies a structurally rejected request
without counting as a failed login.

The signed scheme deserves spelling out, because every part of it is load
bearing. Two RSA keys are published unauthenticated: `?form=keys` carries the
password key, `?form=auth` the signing key and a session sequence number. The
login body is a form string `operation=login&password=<RSA(pwd)>&confirm=true`
AES-128-CBC encrypted with a key/iv the client invents; the RSA is PKCS#1
v1.5. The signature is `k=<key>&i=<iv>&h=<md5(user+pwd)>&s=<seq+len(data)>`,
PKCS#1-encrypted with the signing key in 53-byte chunks. The reply's `data`
field is AES-encrypted with the same key/iv and hides the `stok`; the
`sysauth` cookie riding on that reply must be replayed on every later request
(read from the header directly — the shared cookie jar drops cookies from
bare-IP hosts). Those later requests are wrapped the same way, with the
shorter `h=…&s=…` signature. Every authenticated exchange, the client list
included, goes through that envelope.

The client list comes from `admin/status?form=client_status`, whose entries
carry a `wire_type` of `wired`, `2.4G` or `5G` — the band, stated outright.

The AES half is taken from `cryptography`, which Home Assistant itself
depends on; if it were unavailable the signed variant is skipped rather than
breaking the provider.
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
        # Signed-variant session state, all minted at login time.
        self._seq: int | None = None
        self._sign_key: tuple[int, int] | None = None
        self._aes_key: str | None = None
        self._aes_iv: str | None = None
        self._sysauth: str | None = None

    # ── transport ────────────────────────────────────────────────────────
    def _headers(self, login: bool = False) -> dict[str, str]:
        """What the browser would send; some firmwares 403 without it."""
        headers = {"Referer": f"{self._base}/webpages/index.html"}
        if not login:
            headers["Origin"] = self._base
            if self._sysauth:
                # Replayed by hand: the shared cookie jar refuses to store
                # cookies a bare-IP host sets, and the router insists on it.
                headers["Cookie"] = f"sysauth={self._sysauth}"
        return headers

    async def _post(
        self, path: str, data: dict[str, Any], as_json: bool = False
    ) -> dict[str, Any]:
        payload, _ = await self._post_response(path, data, as_json=as_json)
        return payload

    async def _post_response(
        self,
        path: str,
        data: dict[str, Any],
        as_json: bool = False,
        login: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        kwargs: dict[str, Any] = {"json": data} if as_json else {"data": data}
        try:
            resp = await self._session.post(
                f"{self._base}{path}",
                headers=self._headers(login=login),
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
        return (payload if isinstance(payload, dict) else {}), resp

    async def _post_either_envelope(
        self, path: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Post the fields form-encoded, falling back to the JSON envelope.

        The plain and rsa firmware generations differ in the wrapper as well
        as the payload: older builds take `operation=login&…` form fields,
        newer ones want `{"method": "do", "login": {…}}`. A 403 means the
        wrapper was not understood, which is not a failed login and does not
        count against the router's attempt limit — so retrying the other
        shape is safe. Anything that comes back as JSON is an answer, and is
        returned as-is.
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
        self._sysauth = None
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

        if (
            sign_key is not None
            and sequence is not None
            and password_key is not None
            and _aes_tools()
        ):
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

        assert key is not None
        try:
            return await self._attempt_signed(key, auth)
        except RouterConnectionError as err:
            if "403" not in str(err):
                raise
            # A 403 rejects the request before any credential check — the
            # session sequence went stale between reading it and using it.
            # Fresh key material and one more go; still one counted attempt.
            _LOGGER.debug("Signed login answered 403; refreshing seq and retrying")
            keys_payload = await self._read_or_empty("keys")
            auth = await self._read_or_empty("auth")
            fresh_key = _rsa_key(keys_payload)
            if fresh_key is None:
                raise
            return await self._attempt_signed(fresh_key, auth)

    async def _attempt_signed(
        self, password_key: tuple[int, int], auth: dict[str, Any]
    ) -> dict[str, Any]:
        sign_key = _rsa_key(auth, field="key")
        sequence = _to_int((auth.get("data") or {}).get("seq"))
        if sign_key is None or sequence is None:
            raise RouterConnectionError(
                "Router stopped publishing its signing key mid-login"
            )

        # A fresh AES-128-CBC key/iv per login, as digit strings — the UI
        # invents them the same way, and they double as the session cipher for
        # every reply and every later request.
        self._aes_key = _digits(16)
        self._aes_iv = _digits(16)
        self._sign_key = sign_key
        self._seq = sequence
        self._sysauth = None

        crypted_pwd = _rsa_encrypt_pkcs1(self._password.encode(), *password_key)
        body = f"operation=login&password={crypted_pwd}&confirm=true"
        payload, resp = await self._post_response(
            "/cgi-bin/luci/;stok=/login?form=login",
            self._wrap(body, is_login=True),
            login=True,
        )
        self._sysauth = _sysauth_of(resp)
        return self._unwrap(payload)

    def _wrap(self, body: str, is_login: bool) -> dict[str, str]:
        """The signed envelope: AES-encrypted body plus an RSA signature."""
        aes = _aes_tools()
        assert (
            aes is not None
            and self._aes_key is not None
            and self._aes_iv is not None
            and self._sign_key is not None
            and self._seq is not None
        )
        encrypt, _ = aes
        data = encrypt(self._aes_key, self._aes_iv, body)
        digest = hashlib.md5(
            f"{self._username}{self._password}".encode()
        ).hexdigest()
        confirm = self._seq + len(data)
        if is_login:
            plain = f"k={self._aes_key}&i={self._aes_iv}&h={digest}&s={confirm}"
        else:
            plain = f"h={digest}&s={confirm}"
        return {
            "sign": _rsa_encrypt_pkcs1(plain.encode(), *self._sign_key),
            "data": data,
        }

    def _unwrap(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Decrypt a signed-scheme reply; anything else passes through."""
        data = payload.get("data")
        if not isinstance(data, str) or not data:
            return payload
        aes = _aes_tools()
        if aes is None or self._aes_key is None or self._aes_iv is None:
            return payload
        _, decrypt = aes
        try:
            inner = json.loads(decrypt(self._aes_key, self._aes_iv, data))
        except (ValueError, UnicodeDecodeError):
            _LOGGER.debug("TP-Link reply data did not decrypt; leaving as-is")
            return payload
        return inner if isinstance(inner, dict) else payload

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
        for path, operation in (
            (f"/cgi-bin/luci/;stok={self._stok}/admin/status?form=client_status",
             "read"),
            (f"/cgi-bin/luci/;stok={self._stok}/admin/wireless?form=statistics",
             "load"),
        ):
            try:
                if self._variant == "signed":
                    payload = self._unwrap(
                        await self._post(path, self._wrap(f"operation={operation}",
                                                          is_login=False))
                    )
                else:
                    payload = await self._post(path, {"operation": operation})
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


def _sysauth_of(resp: Any) -> str | None:
    """The session cookie, read off the reply itself.

    The shared aiohttp jar refuses cookies set by bare-IP hosts, which is
    exactly what a router is — so the jar never has it and the header is the
    only place to look.
    """
    for header in resp.headers.getall("Set-Cookie", []):
        first = header.split(";", 1)[0]
        name, _, value = first.partition("=")
        if name.strip() == "sysauth" and value:
            return value.strip()
    return None


def _login_error(payload: dict[str, Any], variant: str) -> str:
    """Say what the router said, including how many attempts are left.

    These routers lock the account after a fixed number of failures, so the
    remaining budget is the most useful thing to pass on.
    """
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
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
    """Textbook RSA over zero-padded blocks, hex-encoded.

    This is what the *older* rsa-variant firmwares do; the signed scheme uses
    PKCS#1 v1.5 instead (below), and mixing the two up is rejected without an
    error code.
    """
    key_len = (n.bit_length() + 7) // 8
    out: list[str] = []
    for start in range(0, len(data), key_len):
        block = data[start : start + key_len]
        cipher = pow(int.from_bytes(block, "big"), e, n)
        out.append(cipher.to_bytes(key_len, "big").hex())
    return "".join(out)


def _rsa_encrypt_pkcs1(data: bytes, n: int, e: int) -> str:
    """RSA with PKCS#1 v1.5 padding, chunked and hex-concatenated.

    What the signed-variant UI does (jsbn's `encrypt`): each chunk of at most
    modulus−11 bytes gets the 00 02 <random nonzero…> 00 frame before the
    exponentiation. Done by hand because it must hold even where the crypto
    library refuses small RSA keys — the signing key really is 512 bits.
    """
    key_len = (n.bit_length() + 7) // 8
    max_chunk = key_len - 11
    out: list[str] = []
    for start in range(0, len(data), max_chunk):
        block = data[start : start + max_chunk]
        pad = bytearray()
        while len(pad) < key_len - len(block) - 3:
            pad.extend(b for b in os.urandom(16) if b)
        padded = b"\x00\x02" + bytes(pad[: key_len - len(block) - 3]) + b"\x00" + block
        cipher = pow(int.from_bytes(padded, "big"), e, n)
        out.append(cipher.to_bytes(key_len, "big").hex())
    return "".join(out)


def _digits(length: int) -> str:
    return "".join(str(b % 10) for b in os.urandom(length))


def _aes_tools():
    """AES-128-CBC with PKCS#7 (encrypt, decrypt), or None when unavailable."""
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:  # pragma: no cover - present in every HA install
        return None

    def encrypt(key: str, iv: str, text: str) -> str:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(text.encode()) + padder.finalize()
        cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode()))
        encryptor = cipher.encryptor()
        return base64.b64encode(
            encryptor.update(padded) + encryptor.finalize()
        ).decode()

    def decrypt(key: str, iv: str, encoded: str) -> str:
        raw = base64.b64decode(encoded)
        cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode()))
        decryptor = cipher.decryptor()
        padded = decryptor.update(raw) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode()

    return encrypt, decrypt


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
