"""
Data models for trafikwatch
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class SNMPv3Config:
    """SNMPv3 authentication configuration"""
    username: str = ""
    auth_protocol: str = "sha"       # sha, md5
    auth_password: str = ""
    priv_protocol: str = "aes128"    # aes128, aes192, aes256, des
    priv_password: str = ""

    @property
    def security_level(self) -> str:
        """Infer security level from what's configured"""
        if self.priv_password:
            return "authPriv"
        elif self.auth_password:
            return "authNoPriv"
        return "noAuthNoPriv"


@dataclass
class TargetConfig:
    """A single SNMP target (device) to monitor"""
    host: str
    label: str = ""
    community: str = ""              # override global (v2c)
    port: int = 0                    # override global
    version: str = ""                # per-target override ("2c", "3")
    snmpv3: Optional[SNMPv3Config] = None   # per-target v3 creds
    interfaces: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.label or self.host


@dataclass
class GroupConfig:
    """A named group of targets"""
    name: str
    targets: list[TargetConfig] = field(default_factory=list)


@dataclass
class AppConfig:
    """Top-level application config"""
    community: str = "public"
    version: str = "2c"
    interval: float = 10.0  # seconds
    timeout: float = 5.0
    port: int = 161
    max_history: int = 60   # samples to keep for sparklines
    snmpv3: Optional[SNMPv3Config] = None   # global v3 creds
    groups: list[GroupConfig] = field(default_factory=list)


@dataclass
class RateSample:
    """Single rate measurement for sparkline history"""
    timestamp: datetime = field(default_factory=datetime.now)
    in_rate: float = 0.0   # bits/sec
    out_rate: float = 0.0  # bits/sec


@dataclass
class InterfaceStats:
    """Current and historical stats for a single interface"""
    host: str = ""
    label: str = ""
    if_index: int = 0
    if_name: str = ""
    if_alias: str = ""
    in_octets: int = 0
    out_octets: int = 0
    in_rate: float = 0.0     # bits/sec
    out_rate: float = 0.0    # bits/sec
    speed: int = 0           # bits/sec
    oper_status: int = 0     # 1=up, 2=down, 3=testing
    last_poll: Optional[datetime] = None
    history: list[RateSample] = field(default_factory=list)
    poll_error: str = ""
    max_history: int = 60

    @property
    def display_host(self) -> str:
        return self.label or self.host

    @property
    def util_percent(self) -> float:
        if self.speed == 0:
            return 0.0
        in_util = (self.in_rate / self.speed) * 100
        out_util = (self.out_rate / self.speed) * 100
        return max(in_util, out_util)

    @property
    def status_text(self) -> str:
        return {1: "up", 2: "down", 3: "testing"}.get(self.oper_status, "?")

    def append_sample(self, sample: RateSample) -> None:
        self.history.append(sample)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]


def format_rate(bps: float) -> str:
    """Human-readable rate string"""
    if bps >= 1e9:
        return f"{bps / 1e9:.1f} Gbps"
    elif bps >= 1e6:
        return f"{bps / 1e6:.1f} Mbps"
    elif bps >= 1e3:
        return f"{bps / 1e3:.1f} Kbps"
    else:
        return f"{bps:.0f} bps"


SPARK_CHARS = "▁▂▃▄▅▆▇█"

def sparkline(history: list[RateSample], direction: str = "in", width: int = 8) -> str:
    """Generate a sparkline string from rate history"""
    if not history:
        return ""

    values = [
        s.in_rate if direction == "in" else s.out_rate
        for s in history
    ]

    # Take last N values
    if len(values) > width:
        values = values[-width:]

    peak = max(values) if values else 0
    result = []
    for v in values:
        if peak == 0:
            idx = 0
        else:
            idx = int((v / peak) * (len(SPARK_CHARS) - 1))
            idx = min(idx, len(SPARK_CHARS) - 1)
        result.append(SPARK_CHARS[idx])

    return "".join(result)