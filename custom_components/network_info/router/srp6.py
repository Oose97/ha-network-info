"""Minimal SRP-6 client for Technicolor Homeware gateways.

Self-contained (no pip dependency) reimplementation of the handshake the
gateway's web UI performs. It is SRP-*6*, not 6a — the gateway uses a fixed
multiplier ``k`` rather than ``k = H(N, g)`` — with SHA-256 over the RFC 5054
2048-bit group. The byte encodings below (``_long_to_bytes`` in particular)
match what the gateway hashes on its side; they are deliberately verbatim, so
do not "fix" them to standard big-endian without a device to test against.
"""

from __future__ import annotations

import hashlib
import os

# RFC 5054 2048-bit group.
_N_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CBB4"
    "A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50E8083969EDB767B0CF60"
    "95179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF"
    "747359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481F1D2B907"
    "8717461A5B9D32E688F87748544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB37861"
    "60279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DB"
    "FBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)
_N = int(_N_HEX, 16)
_G = 2
# The gateway's fixed SRP-6 multiplier (not the SRP-6a k = H(N, g)).
_K = int("05b9e8ef059c6b32ea59fc1d322d37f04aa30bae5aa9003b8321e21ddb04e300", 16)


def _long_to_bytes(n: int) -> bytes:
    out = bytearray()
    x = 0
    off = 0
    while x != n:
        b = (n >> off) & 0xFF
        out.append(b)
        x |= b << off
        off += 8
    out.reverse()
    return bytes(out)


def _bytes_to_long(s: bytes) -> int:
    n = 0
    for b in s:
        n = (n << 8) | b
    return n


def _hash_int(*args: int | bytes) -> int:
    h = hashlib.sha256()
    for a in args:
        if a is None:
            continue
        h.update(_long_to_bytes(a) if isinstance(a, int) else a)
    return int(h.hexdigest(), 16)


def _hnxorg() -> bytes:
    hn = hashlib.sha256(_long_to_bytes(_N)).digest()
    hg = hashlib.sha256(_long_to_bytes(_G)).digest()
    return bytes(x ^ y for x, y in zip(hn, hg))


class SRP6Client:
    """One SRP-6 authentication exchange."""

    def __init__(self, username: str, password: str, a: int | None = None) -> None:
        self._i = username
        self._p = password
        self._a = a if a is not None else _bytes_to_long(os.urandom(32)) | (1 << 255)
        self._A = pow(_G, self._a, _N)
        self._M: bytes | None = None
        self._h_amk: bytes | None = None

    @property
    def a_bytes(self) -> bytes:
        """Client public value A, as the gateway expects it."""
        return _long_to_bytes(self._A)

    @property
    def username(self) -> str:
        return self._i

    def process_challenge(self, salt: bytes, b_bytes: bytes) -> bytes | None:
        """Consume the server salt+B, return the client proof M (None on failure)."""
        s = _bytes_to_long(salt)
        b = _bytes_to_long(b_bytes)
        if b % _N == 0:
            return None
        u = _hash_int(self._A, b)
        if u == 0:
            return None
        # gen_x = H(s, H(I:p)); s is hashed as an int via _long_to_bytes.
        x = _hash_int(s, _hash_int(f"{self._i}:{self._p}".encode("latin-1")))
        v = pow(_G, x, _N)
        shared = pow(b - _K * v, self._a + u * x, _N)
        k_hash = hashlib.sha256(_long_to_bytes(shared)).digest()

        m = hashlib.sha256()
        m.update(_hnxorg())
        m.update(hashlib.sha256(self._i.encode("latin-1")).digest())
        m.update(_long_to_bytes(s))
        m.update(_long_to_bytes(self._A))
        m.update(_long_to_bytes(b))
        m.update(k_hash)
        self._M = m.digest()

        amk = hashlib.sha256()
        amk.update(_long_to_bytes(self._A))
        amk.update(self._M)
        amk.update(k_hash)
        self._h_amk = amk.digest()
        return self._M

    def verify_session(self, host_hamk: bytes) -> bool:
        """Confirm the server's proof matches ours."""
        return self._h_amk is not None and self._h_amk == host_hamk
