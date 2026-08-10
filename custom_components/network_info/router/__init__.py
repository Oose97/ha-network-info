"""Router providers — the pluggable, brand-specific side of Network Info.

The integration core only knows this interface: a provider logs into the
router and returns per-client link information (connection path, signal,
router-side name). Adding support for another brand means adding one module
here that implements :class:`RouterProvider`; nothing else changes.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import ClientSession

CATALOG_FILE = Path(__file__).parent / "routers.json"


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


@cache
def load_catalog() -> dict[str, dict[str, Any]]:
    """The router-brand catalog from routers.json, keyed by brand id.

    Each entry describes a supported brand: display name, default gateway,
    whether the config flow must ask for a username and/or password, whether
    to talk HTTPS by default, and the API endpoint shape. Blocking file I/O —
    call via an executor from async code (cached after the first call).
    """
    with CATALOG_FILE.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return {item["id"]: item for item in data.get("routers", [])}


def create_provider(
    brand: str,
    host: str,
    username: str,
    password: str,
    session: ClientSession,
    use_https: bool,
) -> RouterProvider | None:
    """Instantiate the provider for a catalog brand id; None when unknown."""
    # Imported here to avoid an import cycle (providers import from this module).
    from .technicolor import TechnicolorProvider
    from .xiaomi_miwifi import XiaomiMiWiFiProvider

    classes: dict[str, type] = {
        "xiaomi_miwifi": XiaomiMiWiFiProvider,
        "technicolor": TechnicolorProvider,
    }
    cls = classes.get(brand)
    if cls is None:
        return None
    return cls(
        host=host,
        password=password,
        session=session,
        username=username,
        use_https=use_https,
    )
