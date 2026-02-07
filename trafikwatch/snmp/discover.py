"""
Interface discovery for trafikwatch.

Walks a single host and reports all interfaces with status, speed, and alias.
Can output a ready-to-use YAML config snippet.
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass
from typing import Optional

warnings.filterwarnings("ignore", message=".*pysnmp.*deprecated.*")

from pysnmp.hlapi.asyncio import (
    SnmpEngine,
    CommunityData,
    UdpTransportTarget,
    ContextData,
    ObjectType,
    ObjectIdentity,
    walk_cmd,
)

from .engine import (
    OID_IF_NAME,
    OID_IF_DESCR,
    OID_IF_ALIAS,
    OID_IF_HIGH_SPEED,
    OID_IF_OPER_STATUS,
)


def _extract_index(oid_str: str, base_oid: str) -> int | None:
    """
    Extract the ifIndex from a full OID string.

    IF-MIB tables are indexed by a single ifIndex as the last OID component.
    Just grab it — no prefix matching needed.
    """
    try:
        return int(oid_str.split('.')[-1])
    except (ValueError, IndexError):
        return None


@dataclass
class DiscoveredInterface:
    """A single discovered interface"""
    index: int
    name: str
    descr: str = ""
    alias: str = ""
    speed: int = 0       # Mbps
    oper_status: str = ""


async def discover(
    host: str,
    community: str = "public",
    port: int = 161,
    timeout: int = 10,
) -> list[DiscoveredInterface]:
    """Discover all interfaces on a host via SNMP"""
    engine = SnmpEngine()
    credentials = CommunityData(community, mpModel=1)
    transport = await UdpTransportTarget.create(
        (host, port), timeout=timeout, retries=1,
    )

    async def _walk_table(oid: str) -> dict[int, str]:
        results: dict[int, str] = {}
        try:
            async for err_ind, err_status, _, var_binds in walk_cmd(
                engine, credentials, transport, ContextData(),
                ObjectType(ObjectIdentity(oid)),
                lexicographicMode=False,
            ):
                if err_ind or err_status:
                    break
                for vb in var_binds:
                    idx = _extract_index(str(vb[0]), oid)
                    if idx is not None:
                        results[idx] = vb[1].prettyPrint() if hasattr(vb[1], "prettyPrint") else str(vb[1])
        except Exception:
            pass
        return results

    async def _walk_int_table(oid: str) -> dict[int, int]:
        results: dict[int, int] = {}
        try:
            async for err_ind, err_status, _, var_binds in walk_cmd(
                engine, credentials, transport, ContextData(),
                ObjectType(ObjectIdentity(oid)),
                lexicographicMode=False,
            ):
                if err_ind or err_status:
                    break
                for vb in var_binds:
                    idx = _extract_index(str(vb[0]), oid)
                    if idx is not None:
                        try:
                            results[idx] = int(vb[1])
                        except (ValueError, TypeError):
                            continue
        except Exception:
            pass
        return results

    # Walk all tables concurrently
    names_task = _walk_table(OID_IF_NAME)
    descrs_task = _walk_table(OID_IF_DESCR)
    aliases_task = _walk_table(OID_IF_ALIAS)
    speeds_task = _walk_int_table(OID_IF_HIGH_SPEED)
    statuses_task = _walk_int_table(OID_IF_OPER_STATUS)

    names, descrs, aliases, speeds, statuses = await asyncio.gather(
        names_task, descrs_task, aliases_task, speeds_task, statuses_task,
    )

    # Fall back to ifDescr if ifName is empty
    if not names:
        names = descrs

    # Assemble
    interfaces: list[DiscoveredInterface] = []
    for idx, name in sorted(names.items()):
        status_int = statuses.get(idx, 0)
        status_str = {1: "up", 2: "down", 3: "testing"}.get(status_int, f"unknown({status_int})")

        interfaces.append(DiscoveredInterface(
            index=idx,
            name=name,
            descr=descrs.get(idx, ""),
            alias=aliases.get(idx, ""),
            speed=speeds.get(idx, 0),
            oper_status=status_str,
        ))

    return interfaces


def format_table(host: str, interfaces: list[DiscoveredInterface]) -> str:
    """Format discovered interfaces as a readable table"""
    lines = [
        f"\n  Host: {host} — {len(interfaces)} interfaces discovered\n",
        f"  {'Index':<8} {'ifName':<24} {'ifDescr':<24} {'ifAlias':<30} {'Speed':>10}  {'Status':<6}",
        f"  {'─'*8} {'─'*24} {'─'*24} {'─'*30} {'─'*10}  {'─'*6}",
    ]

    for iface in interfaces:
        speed_str = ""
        if iface.speed > 0:
            speed_str = f"{iface.speed // 1000} Gbps" if iface.speed >= 1000 else f"{iface.speed} Mbps"

        lines.append(
            f"  {iface.index:<8} {iface.name:<24} {iface.descr:<24} "
            f"{iface.alias:<30} {speed_str:>10}  {iface.oper_status:<6}"
        )

    return "\n".join(lines)


def generate_yaml(
    host: str,
    community: str,
    interfaces: list[DiscoveredInterface],
    up_only: bool = True,
) -> str:
    """Generate a YAML config snippet for discovered interfaces"""
    lines = [
        f'      - host: "{host}"',
        f'        label: "{host}"',
        f'        interfaces:',
    ]

    skip_prefixes = ("loopback", "lo", "null", "unrouted")
    for iface in interfaces:
        if up_only and iface.oper_status != "up":
            continue
        if iface.name.lower().startswith(skip_prefixes):
            continue
        lines.append(f'          - "{iface.name}"')

    return "\n".join(lines)