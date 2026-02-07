"""
Textual TUI dashboard for trafikwatch.

Layout (normal):
  ┌─ Title Bar ──────────────────────────────────────────┐
  │ ⚡ trafikwatch                                        │
  ├──────────────────────────────────────────────────────┤
  │ ─ Group Name                                         │
  │ Device       Interface    In     ▂▃▅   Out    ▃▅▇   │
  ├──────────────────────────────────────────────────────┤
  │ ↻ 10s │ Last: 14:32:05 │ 2/2 OK │ q:quit r:refresh │
  └──────────────────────────────────────────────────────┘

Layout (with detail):
  ┌─ Title Bar ──────────────────────────────────────────┐
  │ ⚡ trafikwatch                                        │
  ├─────────────── table (60%) ─────────────────────────┤
  │ ─ Group Name                                         │
  │ Device       Interface    In     ▂▃▅   Out    ▃▅▇   │
  ├─────────────── detail (40%) ────────────────────────┤
  │ ▸ edge1-01.iad1  et-0/1/9  CORE::edge5-01.iad1     │
  │ ┌─ Inbound (Gbps) ─────────────────────────────┐    │
  │ │ 35 ┤      ╭─╮                                │    │
  │ │    ┤  ╭──╯   ╰──╮                            │    │
  │ │ 30 ┤─╯           ╰──                         │    │
  │ └───────────────────────────────────────────────┘    │
  │ ┌─ Outbound (Gbps) ────────────────────────────┐    │
  │ │ 38 ┤──╮                                      │    │
  │ │    ┤   ╰─╮    ╭──                            │    │
  │ │ 35 ┤     ╰──╯                                │    │
  │ └───────────────────────────────────────────────┘    │
  ├──────────────────────────────────────────────────────┤
  │ ↻ 10s │ Last: 14:32:05 │ 2/2 OK │ esc:close        │
  └──────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Static
from textual.worker import Worker

try:
    from textual_plotext import PlotextPlot
    HAS_PLOTEXT = True
except ImportError:
    HAS_PLOTEXT = False

from ..config import AppConfig
from ..models import InterfaceStats, format_rate, sparkline
from ..snmp.engine import SNMPPoller


CSS_PATH = Path(__file__).parent / "theme.tcss"


def _sanitize_id(name: str) -> str:
    """Sanitize a string for use as a Textual widget ID"""
    return re.sub(r'[^a-zA-Z0-9_-]', '-', name).strip('-')


def _pick_scale(rates: list[float]) -> tuple[float, str]:
    """Pick a consistent human-readable scale for a list of bps values"""
    if not rates:
        return 1_000_000, "Mbps"
    peak = max(rates)
    if peak >= 1_000_000_000:
        return 1_000_000_000, "Gbps"
    elif peak >= 1_000_000:
        return 1_000_000, "Mbps"
    elif peak >= 1_000:
        return 1_000, "Kbps"
    else:
        return 1, "bps"


# ─────────────────────────────────────────────────────────────────────────────
# Widgets
# ─────────────────────────────────────────────────────────────────────────────

class TitleBar(Static):
    """Top title bar"""
    pass


class StatusBar(Static):
    """Bottom status bar with poll info"""
    pass


class GroupTitle(Static):
    """Group header label"""
    pass


class DetailHeader(Static):
    """Header line inside the detail panel"""
    pass


class TrafikWatchApp(App):
    """Main TUI application"""

    CSS_PATH = CSS_PATH
    TITLE = "trafikwatch"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "dismiss", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, cfg: AppConfig, poller: SNMPPoller):
        super().__init__()
        self.cfg = cfg
        self.poller = poller
        self._tables: dict[str, DataTable] = {}
        self._last_poll: Optional[datetime] = None
        self._selected_key: Optional[str] = None
        self._detail_visible: bool = False

    def compose(self) -> ComposeResult:
        yield TitleBar("⚡ trafikwatch", id="title-bar")

        with Vertical(id="main-container"):
            with VerticalScroll(id="table-scroll"):
                for group in self.cfg.groups:
                    with Vertical(classes="group-container"):
                        yield GroupTitle(f"─ {group.name} ", classes="group-title")
                        table_id = _sanitize_id(f"table-{group.name}")
                        table = DataTable(id=table_id, zebra_stripes=True)
                        yield table

            # Detail panel — only rendered if plotext is available
            if HAS_PLOTEXT:
                with Vertical(id="detail-panel"):
                    yield DetailHeader("", id="detail-header")
                    yield PlotextPlot(id="chart-in")
                    yield PlotextPlot(id="chart-out")

        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        """Set up tables and start polling"""
        # Hide detail panel initially
        if HAS_PLOTEXT:
            self.query_one("#detail-panel").display = False

        for group in self.cfg.groups:
            table_id = _sanitize_id(f"table-{group.name}")
            table = self.query_one(f"#{table_id}", DataTable)
            table.cursor_type = "row"

            # Define columns
            table.add_column("Device", width=18, key="device")
            table.add_column("Interface", width=20, key="interface")
            table.add_column("Description", width=26, key="descr")
            table.add_column("Status", width=6, key="status")
            table.add_column("In", width=12, key="in_rate")
            table.add_column("▃", width=8, key="in_spark")
            table.add_column("Out", width=12, key="out_rate")
            table.add_column("▃", width=8, key="out_spark")
            table.add_column("Util%", width=7, key="util")

            # Add placeholder rows
            for target in group.targets:
                for if_name in target.interfaces:
                    row_key = f"{target.host}:{if_name}"
                    table.add_row(
                        target.display_name,
                        if_name,
                        "",
                        "…",
                        "—",
                        "",
                        "—",
                        "",
                        "—",
                        key=row_key,
                    )

            self._tables[group.name] = table

        # Resolve interfaces and start polling
        self.run_worker(self._startup(), exclusive=True, group="poll")

    # ─────────────────────────────────────────────────────────────────────────
    # Polling lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    async def _startup(self) -> None:
        """Resolve interfaces then kick off first poll"""
        await self.poller.resolve_interfaces()
        await self._do_poll()
        self.set_interval(self.cfg.interval, self._poll_tick)

    def _poll_tick(self) -> None:
        self._start_poll()

    def _start_poll(self) -> None:
        self.run_worker(self._do_poll(), exclusive=True, group="poll")

    async def _do_poll(self) -> None:
        """Execute one poll cycle and update the display"""
        await self.poller.poll_once()
        self._last_poll = datetime.now()
        self._update_tables()
        self._update_status_bar()
        # Refresh charts if detail panel is open
        if self._detail_visible and self._selected_key:
            self._update_charts()

    # ─────────────────────────────────────────────────────────────────────────
    # Table updates
    # ─────────────────────────────────────────────────────────────────────────

    def _update_tables(self) -> None:
        """Refresh all table cells from current stats"""
        grouped = self.poller.get_stats()

        for group_name, stats_list in grouped.items():
            table = self._tables.get(group_name)
            if table is None:
                continue

            for s in stats_list:
                row_key = f"{s.host}:{s.if_name}"

                if s.poll_error:
                    try:
                        table.update_cell(row_key, "device", s.display_host)
                        table.update_cell(row_key, "interface", s.if_name)
                        table.update_cell(row_key, "descr", Text(s.if_alias[:26], style="#666666") if s.if_alias else "")
                        table.update_cell(row_key, "status", _styled_status("err"))
                        table.update_cell(row_key, "in_rate", Text(s.poll_error[:12], style="red"))
                        table.update_cell(row_key, "in_spark", "")
                        table.update_cell(row_key, "out_rate", "")
                        table.update_cell(row_key, "out_spark", "")
                        table.update_cell(row_key, "util", "")
                    except Exception:
                        pass
                    continue

                in_str = format_rate(s.in_rate)
                out_str = format_rate(s.out_rate)
                in_spark = sparkline(s.history, "in", 8)
                out_spark = sparkline(s.history, "out", 8)
                util = s.util_percent

                try:
                    table.update_cell(row_key, "device", s.display_host)
                    table.update_cell(row_key, "interface", s.if_name)
                    table.update_cell(row_key, "descr", Text(s.if_alias[:26], style="#666666") if s.if_alias else "")
                    table.update_cell(row_key, "status", _styled_status(s.status_text))
                    table.update_cell(row_key, "in_rate", Text(in_str, style="#00ff88"))
                    table.update_cell(row_key, "in_spark", Text(in_spark, style="#00d4ff"))
                    table.update_cell(row_key, "out_rate", Text(out_str, style="#00ff88"))
                    table.update_cell(row_key, "out_spark", Text(out_spark, style="#00d4ff"))
                    table.update_cell(row_key, "util", _styled_util(util))
                except Exception:
                    pass

    def _update_status_bar(self) -> None:
        """Update the status bar"""
        total, healthy = self.poller.device_count()
        interval = f"{self.cfg.interval:.0f}s"
        last = self._last_poll.strftime("%H:%M:%S") if self._last_poll else "never"

        if self._detail_visible:
            hints = "esc:close  r:refresh  q:quit"
        elif HAS_PLOTEXT:
            hints = "enter:detail  r:refresh  q:quit"
        else:
            hints = "q:quit  r:refresh"

        bar = self.query_one("#status-bar", StatusBar)
        bar.update(
            f"  ↻ {interval} │ Last poll: {last} │ "
            f"{healthy}/{total} devices OK │ {hints}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Detail panel — split-screen interface history
    # ─────────────────────────────────────────────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on a table row — open detail panel"""
        if not HAS_PLOTEXT:
            return

        row_key = str(event.row_key.value)
        self._selected_key = row_key
        self._show_detail()
        self._update_charts()

    def _show_detail(self) -> None:
        """Show the detail panel and shrink the table area"""
        panel = self.query_one("#detail-panel")
        panel.display = True
        scroll = self.query_one("#table-scroll")
        scroll.add_class("split")
        self._detail_visible = True
        self._update_status_bar()

    def _hide_detail(self) -> None:
        """Hide the detail panel and restore the table area"""
        panel = self.query_one("#detail-panel")
        panel.display = False
        scroll = self.query_one("#table-scroll")
        scroll.remove_class("split")
        self._detail_visible = False
        self._selected_key = None
        self._update_status_bar()

    def _update_charts(self) -> None:
        """Redraw the in/out charts from the selected interface's history"""
        if not self._selected_key or not HAS_PLOTEXT:
            return

        stats = self.poller.get_interface_stats(self._selected_key)
        if stats is None or not stats.history:
            return

        now = datetime.now()

        # Build data arrays from history ring buffer
        times = [-(now - sample.timestamp).total_seconds() / 60 for sample in stats.history]
        in_rates = [sample.in_rate for sample in stats.history]
        out_rates = [sample.out_rate for sample in stats.history]

        # Pick a consistent scale (Gbps/Mbps/Kbps) across both directions
        all_rates = in_rates + out_rates
        divisor, unit = _pick_scale(all_rates)
        in_scaled = [r / divisor for r in in_rates]
        out_scaled = [r / divisor for r in out_rates]

        # Y-axis ceiling — 10% headroom above peak, minimum 0.1 to avoid flat line
        y_max_in = max(max(in_scaled) * 1.1, 0.1) if in_scaled else 0.1
        y_max_out = max(max(out_scaled) * 1.1, 0.1) if out_scaled else 0.1

        # Current rates for the header
        cur_in = format_rate(in_rates[-1]) if in_rates else "—"
        cur_out = format_rate(out_rates[-1]) if out_rates else "—"

        # Update header
        header = self.query_one("#detail-header", DetailHeader)
        alias = f"  {stats.if_alias}" if stats.if_alias else ""
        header.update(
            f"  ▸ {stats.display_host}  {stats.if_name}{alias}"
            f"    │  In: {cur_in}  Out: {cur_out}"
        )

        # ── Inbound chart ──
        chart_in = self.query_one("#chart-in", PlotextPlot)
        plt_in = chart_in.plt
        plt_in.clear_data()
        plt_in.clear_figure()
        plt_in.theme("dark")
        plt_in.plot(times, in_scaled, color=(0, 255, 136))
        plt_in.title(f"Inbound ({unit})")
        plt_in.xlabel("Minutes ago")
        plt_in.xlim(-60, 0)
        plt_in.ylim(0, y_max_in)
        chart_in.refresh()

        # ── Outbound chart ──
        chart_out = self.query_one("#chart-out", PlotextPlot)
        plt_out = chart_out.plt
        plt_out.clear_data()
        plt_out.clear_figure()
        plt_out.theme("dark")
        plt_out.plot(times, out_scaled, color=(0, 212, 255))
        plt_out.title(f"Outbound ({unit})")
        plt_out.xlabel("Minutes ago")
        plt_out.xlim(-60, 0)
        plt_out.ylim(0, y_max_out)
        chart_out.refresh()

    # ─────────────────────────────────────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        """Force an immediate refresh"""
        self._start_poll()

    def action_dismiss(self) -> None:
        """Escape key — close detail if open, otherwise quit"""
        if self._detail_visible:
            self._hide_detail()
        else:
            self.action_quit()

    def action_quit(self) -> None:
        """Clean shutdown"""
        self.poller.shutdown()
        self.exit()


# ─────────────────────────────────────────────────────────────────────────────
# Rich text helpers
# ─────────────────────────────────────────────────────────────────────────────

def _styled_status(status: str) -> Text:
    """Color-code interface status"""
    colors = {
        "up": "#00ff88",
        "down": "#ff4444",
        "testing": "#ffcc00",
        "err": "#ff4444",
    }
    return Text(status, style=colors.get(status, "#666666"))


def _styled_util(util: float) -> Text:
    """Color-code utilization percentage"""
    s = f"{util:.0f}%"
    if util > 80:
        return Text(s, style="#ff4444")
    elif util > 50:
        return Text(s, style="#ffcc00")
    elif util > 0:
        return Text(s, style="#00ff88")
    else:
        return Text(s, style="#666666")