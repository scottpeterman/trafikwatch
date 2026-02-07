"""
SNMP polling engine optimized for repeated polling.

Key differences from one-shot snmp_ops.py:
- Single shared SnmpEngine instance (expensive to create)
- Transport cache per host:port (reuse UDP sockets)
- Batch GET for all interface OIDs on a target in one PDU
- Counter wrap / reset detection
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from datetime import datetime
from typing import Optional

warnings.filterwarnings("ignore", message=".*pysnmp.*deprecated.*")

from pysnmp.hlapi.asyncio import (
    SnmpEngine,
    CommunityData,
    UsmUserData,
    UdpTransportTarget,
    ContextData,
    ObjectType,
    ObjectIdentity,
    get_cmd,
    walk_cmd,
    usmHMACSHAAuthProtocol,
    usmHMACMD5AuthProtocol,
    usmAesCfb128Protocol,
    usmAesCfb192Protocol,
    usmAesCfb256Protocol,
    usmDESPrivProtocol,
    usmNoAuthProtocol,
    usmNoPrivProtocol,
)

from ..models import AppConfig, TargetConfig, SNMPv3Config, InterfaceStats, RateSample

log = logging.getLogger("trafikwatch.snmp")

# Standard IF-MIB OIDs
OID_IF_DESCR       = "1.3.6.1.2.1.2.2.1.2"
OID_IF_NAME        = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_ALIAS       = "1.3.6.1.2.1.31.1.1.1.18"
OID_IF_HC_IN       = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT      = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_HIGH_SPEED  = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"

# SNMPv3 protocol mappings
AUTH_PROTOCOLS = {
    "sha":  usmHMACSHAAuthProtocol,
    "md5":  usmHMACMD5AuthProtocol,
    "none": usmNoAuthProtocol,
}
PRIV_PROTOCOLS = {
    "aes":    usmAesCfb128Protocol,
    "aes128": usmAesCfb128Protocol,
    "aes192": usmAesCfb192Protocol,
    "aes256": usmAesCfb256Protocol,
    "des":    usmDESPrivProtocol,
    "none":   usmNoPrivProtocol,
}

# Union type for credentials
SnmpCredentials = CommunityData | UsmUserData


class SNMPPoller:
    """
    Manages SNMP polling for all configured targets.

    Shares a single SnmpEngine and caches transports per host.
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.engine = SnmpEngine()
        self._transport_cache: dict[str, UdpTransportTarget] = {}
        self._if_indexes: dict[str, dict[str, int]] = {}  # host -> {ifName: ifIndex}
        self._stats: dict[str, InterfaceStats] = {}        # "host:ifName" -> stats
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Transport management
    # -------------------------------------------------------------------------

    async def _get_transport(self, host: str, port: int, timeout: float) -> UdpTransportTarget:
        """Get or create a cached transport for a host:port"""
        key = f"{host}:{port}"
        if key not in self._transport_cache:
            log.debug(f"Creating transport for {key} (timeout={timeout}s)")
            self._transport_cache[key] = await UdpTransportTarget.create(
                (host, port), timeout=timeout, retries=1,
            )
        return self._transport_cache[key]

    def _get_credentials(self, target: TargetConfig) -> SnmpCredentials:
        """Build v2c or v3 credentials for a target"""
        version = target.version or self.cfg.version

        if version == "3":
            v3cfg = target.snmpv3 or self.cfg.snmpv3
            if not v3cfg or not v3cfg.username:
                raise ValueError(f"{target.host}: version 3 requires snmpv3 config with username")

            auth_proto = AUTH_PROTOCOLS.get(v3cfg.auth_protocol.lower(), usmNoAuthProtocol)
            priv_proto = PRIV_PROTOCOLS.get(v3cfg.priv_protocol.lower(), usmNoPrivProtocol)

            log.debug(
                f"{target.host}: v3 user={v3cfg.username} "
                f"auth={v3cfg.auth_protocol} priv={v3cfg.priv_protocol} "
                f"level={v3cfg.security_level}"
            )

            return UsmUserData(
                v3cfg.username,
                authKey=v3cfg.auth_password or None,
                privKey=v3cfg.priv_password or None,
                authProtocol=auth_proto,
                privProtocol=priv_proto,
            )

        # v2c / v1
        community = target.community or self.cfg.community
        mp_model = 1 if version == "2c" else 0
        return CommunityData(community, mpModel=mp_model)

    # -------------------------------------------------------------------------
    # Interface resolution (one-time walk at startup)
    # -------------------------------------------------------------------------

    async def resolve_interfaces(self) -> None:
        """Walk ifName/ifDescr tables on all targets to map names → ifIndex"""
        tasks = []
        for group in self.cfg.groups:
            for target in group.targets:
                log.info(f"Resolving interfaces on {target.host} ({target.label})")
                tasks.append(self._resolve_target(target))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.warning(f"Failed to resolve interfaces: {result}")

    async def _resolve_target(self, target: TargetConfig) -> None:
        """Resolve interface names to ifIndex for a single target"""
        port = target.port or self.cfg.port
        transport = await self._get_transport(target.host, port, self.cfg.timeout)
        credentials = self._get_credentials(target)

        # Walk ifName first (preferred), fall back to ifDescr
        log.debug(f"{target.host}: walking ifName table")
        name_map = await self._walk_string_table(target.host, transport, credentials, OID_IF_NAME)
        log.debug(f"{target.host}: ifName returned {len(name_map)} entries")

        if not name_map:
            log.debug(f"{target.host}: ifName empty, falling back to ifDescr")
            name_map = await self._walk_string_table(target.host, transport, credentials, OID_IF_DESCR)
            log.debug(f"{target.host}: ifDescr returned {len(name_map)} entries")

        if not name_map:
            log.warning(f"{target.host}: no interfaces found via ifName or ifDescr — SNMP reachable?")
            return

        # Build reverse lookup: name → ifIndex
        index_map: dict[str, int] = {}
        for idx, name in name_map.items():
            index_map[name] = idx

        # Log all discovered interface names for debugging config mismatches
        log.debug(f"{target.host}: ifName entries: {dict(sorted(name_map.items()))}")

        # Also check ifDescr for devices with different naming conventions
        descr_map = await self._walk_string_table(target.host, transport, credentials, OID_IF_DESCR)
        for idx, name in descr_map.items():
            if name not in index_map:
                index_map[name] = idx

        if descr_map:
            log.debug(f"{target.host}: ifDescr entries: {dict(sorted(descr_map.items()))}")

        # Walk ifAlias (user-configured interface descriptions)
        alias_map = await self._walk_string_table(target.host, transport, credentials, OID_IF_ALIAS)

        async with self._lock:
            self._if_indexes[target.host] = index_map

            # Initialize stats entries for configured interfaces
            for if_name in target.interfaces:
                key = f"{target.host}:{if_name}"
                if key not in self._stats:
                    self._stats[key] = InterfaceStats(
                        host=target.host,
                        label=target.label,
                        if_name=if_name,
                        max_history=self.cfg.max_history,
                    )
                if if_name in index_map:
                    self._stats[key].if_index = index_map[if_name]
                    log.debug(f"{target.host}: '{if_name}' → ifIndex {index_map[if_name]}")
                    # Attach ifAlias if available
                    idx = index_map[if_name]
                    if idx in alias_map and alias_map[idx]:
                        self._stats[key].if_alias = alias_map[idx]
                        log.debug(f"{target.host}: '{if_name}' alias='{alias_map[idx]}'")
                else:
                    self._stats[key].poll_error = "interface not found"
                    # Show what names ARE available so the user can fix their config
                    available = sorted(index_map.keys())
                    log.warning(
                        f"{target.host}: interface '{if_name}' NOT FOUND in SNMP tables. "
                        f"Available names: {available}"
                    )

        found = sum(1 for n in target.interfaces if n in index_map)
        log.info(f"{target.host}: resolved {found}/{len(target.interfaces)} interfaces")

    async def _walk_string_table(
        self,
        host: str,
        transport: UdpTransportTarget,
        credentials: SnmpCredentials,
        oid: str,
    ) -> dict[int, str]:
        """Walk an OID table and return {ifIndex: string_value}"""
        results: dict[int, str] = {}

        try:
            async for error_ind, error_status, _, var_binds in walk_cmd(
                self.engine,
                credentials,
                transport,
                ContextData(),
                ObjectType(ObjectIdentity(oid)),
                lexicographicMode=False,
            ):
                if error_ind:
                    log.warning(f"Walk {host} {oid}: error_ind={error_ind}")
                    break
                if error_status:
                    log.warning(f"Walk {host} {oid}: error_status={error_status.prettyPrint()}")
                    break

                for var_bind in var_binds:
                    try:
                        idx = int(str(var_bind[0]).split('.')[-1])
                    except (ValueError, IndexError):
                        log.debug(f"Walk {host}: couldn't parse index from OID {var_bind[0]}")
                        continue

                    value = var_bind[1]
                    if hasattr(value, "prettyPrint"):
                        results[idx] = value.prettyPrint()
                    elif isinstance(value, bytes):
                        results[idx] = value.decode("utf-8", errors="replace")
                    else:
                        results[idx] = str(value)

        except Exception as e:
            log.warning(f"Walk {host} {oid}: exception {type(e).__name__}: {e}")

        return results

    # -------------------------------------------------------------------------
    # Polling loop
    # -------------------------------------------------------------------------

    async def poll_once(self) -> None:
        """Poll all targets concurrently"""
        tasks = []
        for group in self.cfg.groups:
            for target in group.targets:
                tasks.append(self._poll_target(target))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_target(self, target: TargetConfig) -> None:
        """Poll all configured interfaces on a single target"""
        port = target.port or self.cfg.port

        try:
            transport = await self._get_transport(target.host, port, self.cfg.timeout)
        except Exception as e:
            log.warning(f"{target.host}: transport failed: {type(e).__name__}: {e}")
            await self._set_target_error(target, f"transport error: {e}")
            return

        credentials = self._get_credentials(target)

        async with self._lock:
            host_indexes = self._if_indexes.get(target.host, {})

        if not host_indexes:
            log.debug(f"{target.host}: no resolved indexes, skipping poll")
            return

        now = datetime.now()

        for if_name in target.interfaces:
            key = f"{target.host}:{if_name}"
            idx = host_indexes.get(if_name)
            if idx is None:
                log.debug(f"{target.host}: '{if_name}' has no ifIndex, skipping poll")
                continue

            # Batch GET: HC counters, speed, oper status
            oids = [
                ObjectType(ObjectIdentity(f"{OID_IF_HC_IN}.{idx}")),
                ObjectType(ObjectIdentity(f"{OID_IF_HC_OUT}.{idx}")),
                ObjectType(ObjectIdentity(f"{OID_IF_HIGH_SPEED}.{idx}")),
                ObjectType(ObjectIdentity(f"{OID_IF_OPER_STATUS}.{idx}")),
            ]

            try:
                error_ind, error_status, _, var_binds = await get_cmd(
                    self.engine,
                    credentials,
                    transport,
                    ContextData(),
                    *oids,
                )

                if error_ind:
                    log.warning(f"{key}: GET error_ind={error_ind}")
                    async with self._lock:
                        if key in self._stats:
                            self._stats[key].poll_error = str(error_ind)
                    continue

                if error_status:
                    log.warning(f"{key}: GET error_status={error_status.prettyPrint()}")
                    async with self._lock:
                        if key in self._stats:
                            self._stats[key].poll_error = str(error_status.prettyPrint())
                    continue

                # Log raw varbinds on first poll or at debug level
                log.debug(
                    f"{key}: raw varbinds: "
                    + ", ".join(
                        f"{var_bind[0]}={var_bind[1].prettyPrint() if hasattr(var_bind[1], 'prettyPrint') else var_bind[1]}"
                        for var_bind in var_binds
                    )
                )

                # Check for noSuchObject / noSuchInstance responses
                for var_bind in var_binds:
                    val_str = str(var_bind[1])
                    if "noSuch" in val_str or "No Such" in val_str:
                        log.warning(f"{key}: {var_bind[0]} returned {val_str} — OID not supported?")

                # Parse results
                async with self._lock:
                    s = self._stats.get(key)
                    if s is None:
                        continue

                    prev_in = s.in_octets
                    prev_out = s.out_octets
                    prev_time = s.last_poll

                    parsed_count = 0
                    for var_bind in var_binds:
                        oid_str = str(var_bind[0])
                        value = var_bind[1]

                        # pysnmp 7.x returns ASN.1 objects — use prettyPrint() first
                        try:
                            raw = value.prettyPrint() if hasattr(value, 'prettyPrint') else str(value)
                            val_int = int(raw)
                            parsed_count += 1
                        except (ValueError, TypeError):
                            log.debug(f"{key}: couldn't parse int from {oid_str}={raw!r}")
                            continue

                        if f"{OID_IF_HC_IN}.{idx}" in oid_str:
                            s.in_octets = val_int
                        elif f"{OID_IF_HC_OUT}.{idx}" in oid_str:
                            s.out_octets = val_int
                        elif f"{OID_IF_HIGH_SPEED}.{idx}" in oid_str:
                            # ifHighSpeed is in Mbps, convert to bps
                            s.speed = val_int * 1_000_000
                        elif f"{OID_IF_OPER_STATUS}.{idx}" in oid_str:
                            s.oper_status = val_int
                        else:
                            log.debug(f"{key}: OID {oid_str} didn't match any expected pattern for idx={idx}")

                    if parsed_count < 4:
                        log.debug(f"{key}: only parsed {parsed_count}/4 OID values")

                    # Calculate rates if we have a previous sample
                    if prev_time and prev_in > 0:
                        elapsed = (now - prev_time).total_seconds()
                        if elapsed > 0:
                            delta_in = s.in_octets - prev_in
                            delta_out = s.out_octets - prev_out

                            # Counter wrap/reset detection
                            if delta_in < 0 or delta_out < 0:
                                log.debug(f"{key}: counter reset detected, skipping rate calc")
                            else:
                                s.in_rate = (delta_in * 8) / elapsed
                                s.out_rate = (delta_out * 8) / elapsed
                                log.debug(
                                    f"{key}: in={s.in_rate:.0f}bps out={s.out_rate:.0f}bps "
                                    f"(elapsed={elapsed:.1f}s, delta_in={delta_in}, delta_out={delta_out})"
                                )
                    elif prev_time and prev_in == 0:
                        # First real sample after init — counters were 0 from init, not from device
                        # Still calculate; this avoids skipping the first real interval
                        elapsed = (now - prev_time).total_seconds()
                        if elapsed > 0 and s.in_octets > 0:
                            log.debug(f"{key}: first rate calc (prev counters were init zeros)")
                            s.in_rate = (s.in_octets * 8) / elapsed
                            s.out_rate = (s.out_octets * 8) / elapsed
                    elif not prev_time:
                        log.debug(f"{key}: first poll, storing baseline counters "
                                  f"(in={s.in_octets}, out={s.out_octets})")

                    s.last_poll = now
                    s.poll_error = ""

                    s.append_sample(RateSample(
                        timestamp=now,
                        in_rate=s.in_rate,
                        out_rate=s.out_rate,
                    ))

            except Exception as e:
                async with self._lock:
                    if key in self._stats:
                        self._stats[key].poll_error = f"poll error: {e}"
                log.warning(f"Poll {key}: {type(e).__name__}: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Stats access
    # -------------------------------------------------------------------------

    def get_interface_stats(self, key: str) -> Optional[InterfaceStats]:
        """Get stats for a specific interface by key (host:ifName)"""
        return self._stats.get(key)

    def get_stats(self) -> dict[str, list[InterfaceStats]]:
        """Return stats grouped by config group name"""
        grouped: dict[str, list[InterfaceStats]] = {}

        for group in self.cfg.groups:
            items: list[InterfaceStats] = []
            for target in group.targets:
                for if_name in target.interfaces:
                    key = f"{target.host}:{if_name}"
                    if key in self._stats:
                        items.append(self._stats[key])
            grouped[group.name] = items

        return grouped

    def device_count(self) -> tuple[int, int]:
        """Return (total_devices, healthy_devices)"""
        hosts: set[str] = set()
        healthy: set[str] = set()

        for s in self._stats.values():
            hosts.add(s.host)
            if not s.poll_error:
                healthy.add(s.host)

        return len(hosts), len(healthy)

    async def _set_target_error(self, target: TargetConfig, error: str) -> None:
        """Set error on all interfaces for a target"""
        async with self._lock:
            for if_name in target.interfaces:
                key = f"{target.host}:{if_name}"
                if key in self._stats:
                    self._stats[key].poll_error = error

    def shutdown(self) -> None:
        """Clean up engine resources"""
        self._transport_cache.clear()