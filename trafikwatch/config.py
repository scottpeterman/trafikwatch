"""
YAML config loader

Compatible with the Go trafikwatch config format:

    community: "lab"
    version: "2c"
    interval: 10s
    timeout: 5s
    port: 161

    groups:
      - name: "Arista Aggregation"
        targets:
          - host: "172.17.1.128"
            label: "agg1.iad1"
            interfaces:
              - "Ethernet1"
              - "Ethernet2"

SNMPv3 support:

    version: "3"
    snmpv3:
      username: "cisco"
      auth_protocol: "sha"
      auth_password: "cisco123"
      priv_protocol: "aes128"
      priv_password: "cisco123"

    # Per-target override and mixed v2c/v3:
    groups:
      - name: "Fabric"
        targets:
          - host: "172.16.2.2"
            version: "3"           # override global version
            snmpv3:                 # override global v3 creds (optional)
              username: "monitoring"
              auth_password: "other_pass"
              priv_password: "other_pass"
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import AppConfig, GroupConfig, TargetConfig, SNMPv3Config


def _parse_duration(value) -> float:
    """Parse Go-style duration strings (e.g., '10s', '5m', '1.5s') to seconds"""
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip().lower()
    match = re.match(r'^([\d.]+)\s*(s|ms|m|h)?$', s)
    if not match:
        return float(s)

    num = float(match.group(1))
    unit = match.group(2) or "s"

    multipliers = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return num * multipliers.get(unit, 1.0)


def _parse_v3(raw: dict, fallback: SNMPv3Config | None = None) -> SNMPv3Config | None:
    """Parse an snmpv3 block, merging with fallback for missing fields"""
    if not raw:
        return None

    # Start from fallback values if provided, so per-target only needs to
    # specify the fields that differ from global
    base = fallback or SNMPv3Config()

    return SNMPv3Config(
        username=raw.get("username", base.username),
        auth_protocol=raw.get("auth_protocol", base.auth_protocol),
        auth_password=raw.get("auth_password", base.auth_password),
        priv_protocol=raw.get("priv_protocol", base.priv_protocol),
        priv_password=raw.get("priv_password", base.priv_password),
    )


def load(path: str | Path) -> AppConfig:
    """Load config from YAML file"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ValueError(f"Empty config file: {path}")

    # Global v3 config
    global_v3 = _parse_v3(raw.get("snmpv3", {}))

    cfg = AppConfig(
        community=raw.get("community", "public"),
        version=str(raw.get("version", "2c")),
        interval=_parse_duration(raw.get("interval", 10)),
        timeout=_parse_duration(raw.get("timeout", 5)),
        port=int(raw.get("port", 161)),
        max_history=int(raw.get("max_history", 60)),
        snmpv3=global_v3,
    )

    for g in raw.get("groups", []):
        group = GroupConfig(name=g.get("name", "Default"))
        for t in g.get("targets", []):
            # Per-target v3: merge with global so partial overrides work
            target_v3_raw = t.get("snmpv3", {})
            target_v3 = _parse_v3(target_v3_raw, fallback=global_v3) if target_v3_raw else None

            target = TargetConfig(
                host=t["host"],
                label=t.get("label", ""),
                community=t.get("community", ""),
                port=int(t.get("port", 0)),
                version=str(t.get("version", "")),
                snmpv3=target_v3,
                interfaces=t.get("interfaces", []),
            )
            group.targets.append(target)
        cfg.groups.append(group)

    return cfg