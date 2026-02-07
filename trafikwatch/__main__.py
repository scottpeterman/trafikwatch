"""
trafikwatch CLI entry point.

Usage:
  Monitor:   trafikwatch -config trafikwatch.yaml
  Discover:  trafikwatch -discover 10.0.1.1 -community public
  YAML gen:  trafikwatch -discover 10.0.1.1 -community public -yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import load
from .snmp.discover import discover, format_table, generate_yaml
from .snmp.engine import SNMPPoller
from .tui.app import TrafikWatchApp


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"⚡ trafikwatch v{__version__} — real-time interface monitoring from the terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  Monitor:   trafikwatch --config trafikwatch.yaml
  Discover:  trafikwatch --discover 10.0.1.1 --community public
  YAML gen:  trafikwatch --discover 10.0.1.1 --community public --yaml

SNMPv3:
  Discover:  trafikwatch --discover 10.0.1.1 --snmp-version 3 --v3-user cisco \\
               --v3-auth-password cisco123 --v3-priv-password cisco123
        """,
    )

    parser.add_argument("--config", "-c", default="trafikwatch.yaml", help="path to config file")
    parser.add_argument("--version", "-V", action="store_true", help="print version and exit")

    # Discovery mode
    parser.add_argument("--discover", "-d", metavar="HOST", help="discover interfaces on a host")
    parser.add_argument("--community", default="public", help="SNMP community for discovery (v2c)")
    parser.add_argument("--port", type=int, default=161, help="SNMP port")
    parser.add_argument("--yaml", "-y", action="store_true", help="output YAML config snippet")
    parser.add_argument("--all", "-a", action="store_true", help="include down interfaces")

    # SNMPv3 discovery
    parser.add_argument("--snmp-version", default="2c", choices=["1", "2c", "3"],
                        help="SNMP version (default: 2c)")
    parser.add_argument("--v3-user", default="", help="SNMPv3 username")
    parser.add_argument("--v3-auth-protocol", default="sha", choices=["sha", "md5"],
                        help="SNMPv3 auth protocol (default: sha)")
    parser.add_argument("--v3-auth-password", default="", help="SNMPv3 auth password")
    parser.add_argument("--v3-priv-protocol", default="aes128",
                        choices=["aes", "aes128", "aes192", "aes256", "des"],
                        help="SNMPv3 priv protocol (default: aes128)")
    parser.add_argument("--v3-priv-password", default="", help="SNMPv3 priv password")

    # Debug / Logging
    parser.add_argument("--debug", action="store_true", help="enable debug logging to stderr")
    parser.add_argument("--log", "-l", metavar="FILE", help="log to file at debug level (safe with TUI)")

    args = parser.parse_args()

    if args.version:
        print(f"trafikwatch v{__version__}")
        sys.exit(0)

    # --- Logging setup ---
    # File logging is TUI-safe (doesn't touch stdout/stderr)
    # --debug sends to stderr which conflicts with Textual, so --log is preferred
    if args.log:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s.%(msecs)03d %(name)-24s %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
            filename=args.log,
            filemode="a",
        )
        logging.getLogger("trafikwatch").info("=" * 60)
        logging.getLogger("trafikwatch").info(f"trafikwatch v{__version__} starting — log level DEBUG")
        logging.getLogger("trafikwatch").info(f"config: {args.config}")
        logging.getLogger("trafikwatch").info("=" * 60)
    elif args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
    else:
        logging.basicConfig(level=logging.WARNING)

    # --- Discovery mode ---
    if args.discover:
        version_label = f"v{args.snmp_version}"
        if args.snmp_version == "3":
            if not args.v3_user:
                print("Error: --v3-user is required for SNMPv3 discovery", file=sys.stderr)
                sys.exit(1)
            version_label += f" user={args.v3_user}"

        print(f"⚡ trafikwatch — discovering interfaces on {args.discover} ({version_label})...")
        try:
            interfaces = asyncio.run(
                discover(
                    args.discover,
                    community=args.community,
                    port=args.port,
                    version=args.snmp_version,
                    v3_user=args.v3_user,
                    v3_auth_protocol=args.v3_auth_protocol,
                    v3_auth_password=args.v3_auth_password,
                    v3_priv_protocol=args.v3_priv_protocol,
                    v3_priv_password=args.v3_priv_password,
                )
            )
        except Exception as e:
            print(f"Discovery failed: {e}", file=sys.stderr)
            sys.exit(1)

        if args.yaml:
            print(generate_yaml(args.discover, args.community, interfaces, up_only=not args.all))
        else:
            print(format_table(args.discover, interfaces))
        sys.exit(0)

    # --- Monitor mode ---
    try:
        cfg = load(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(f"Create a config file or use --discover to generate one.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    poller = SNMPPoller(cfg)

    # Launch TUI (resolve happens inside the app's event loop)
    app = TrafikWatchApp(cfg, poller)
    app.run()


if __name__ == "__main__":
    main()