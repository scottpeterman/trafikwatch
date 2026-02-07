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
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import AppConfig, GroupConfig, TargetConfig


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


def load(path: str | Path) -> AppConfig:
    """Load config from YAML file"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ValueError(f"Empty config file: {path}")

    cfg = AppConfig(
        community=raw.get("community", "public"),
        version=str(raw.get("version", "2c")),
        interval=_parse_duration(raw.get("interval", 10)),
        timeout=_parse_duration(raw.get("timeout", 5)),
        port=int(raw.get("port", 161)),
        max_history=int(raw.get("max_history", 60)),
    )

    for g in raw.get("groups", []):
        group = GroupConfig(name=g.get("name", "Default"))
        for t in g.get("targets", []):
            target = TargetConfig(
                host=t["host"],
                label=t.get("label", ""),
                community=t.get("community", ""),
                port=int(t.get("port", 0)),
                interfaces=t.get("interfaces", []),
            )
            group.targets.append(target)
        cfg.groups.append(group)

    return cfg
