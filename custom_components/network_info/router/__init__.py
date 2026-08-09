"""Router providers — the pluggable, brand-specific side of Network Info.

The integration core only knows this interface: a provider logs into the
router and returns per-client link information (connection path, signal,
router-side name). Adding support for another brand means adding one module
here that implements :class:`RouterProvider`; nothing else changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class RouterError(Exception):
    """Base error for router providers."""


class RouterConnectionError(RouterError):
    """Router unreachable or returned an unexpected response."""


class RouterAuthError(RouterError):
    """Router rejected the credentials."""


@dataclass
class RouterClient:
    """Link-level info the router knows about one client."""

    mac: str
    ip: str | None
    name: str | None
    connection: str | None
    signal: int | None
    online: bool


class RouterProvider(ABC):
    """Interface every brand-specific router backend implements."""

    model: str | None = None

    @abstractmethod
    async def async_login(self) -> None:
        """Authenticate against the router. Raises RouterAuthError / RouterConnectionError."""

    @abstractmethod
    async def async_get_clients(self) -> dict[str, RouterClient]:
        """Return clients known to the router, keyed by lowercase MAC."""


def normalize_mac(mac: str | None) -> str | None:
    """Normalize a MAC address to lowercase colon-separated form."""
    if not mac:
        return None
    mac = mac.strip().lower().replace("-", ":")
    return mac or None
