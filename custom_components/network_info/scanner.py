"""Brand-independent network discovery via nmap.

Runs a ping scan (no port probing) over the configured range and returns the
responding hosts with whatever nmap could learn: IP, MAC (requires the ARP
table, available when running as root — the normal case inside the Home
Assistant container), vendor from the MAC OUI, and reverse-DNS hostname.
"""

from __future__ import annotations

from dataclasses import dataclass

import nmap

NMAP_ARGUMENTS = "-sn"


class ScannerError(Exception):
    """Scanning failed (nmap missing or scan error)."""


@dataclass
class ScannedDevice:
    """One host that answered the scan."""

    ip: str
    mac: str | None
    hostname: str | None
    vendor: str | None


def scan_network(ip_range: str) -> list[ScannedDevice]:
    """Scan the given range(s) and return responding devices.

    Blocking — must be run in an executor. ``ip_range`` accepts anything nmap
    does: CIDR ("192.168.1.0/24"), dash ranges ("192.168.1.1-254"), and
    multiple space-separated ranges.
    """
    try:
        scanner = nmap.PortScanner()
    except nmap.PortScannerError as err:
        raise ScannerError(
            f"nmap executable not available: {err}. "
            "The Home Assistant container ships nmap; other installs may need it installed."
        ) from err

    try:
        scanner.scan(hosts=ip_range, arguments=NMAP_ARGUMENTS)
    except nmap.PortScannerError as err:
        raise ScannerError(f"nmap scan of '{ip_range}' failed: {err}") from err

    devices: list[ScannedDevice] = []
    for host in scanner.all_hosts():
        info = scanner[host]
        addresses = info.get("addresses", {})
        ip = addresses.get("ipv4")
        if not ip:
            continue
        mac = addresses.get("mac")
        vendor = None
        if mac:
            vendor = (info.get("vendor") or {}).get(mac) or None
            mac = mac.lower()
        devices.append(
            ScannedDevice(
                ip=ip,
                mac=mac,
                hostname=info.hostname() or None,
                vendor=vendor,
            )
        )
    return devices
