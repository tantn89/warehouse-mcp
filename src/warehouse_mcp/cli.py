"""Command line: `warehouse-mcp --config my.yaml` or `warehouse-mcp --demo`."""

from __future__ import annotations

import argparse
import os
import sys

from warehouse_mcp import __version__
from warehouse_mcp.audit import AuditLog
from warehouse_mcp.backends import BackendError, open_backend
from warehouse_mcp.config import load_settings
from warehouse_mcp.server import build_server
from warehouse_mcp.service import ConfigMismatch, WarehouseService


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="warehouse-mcp", description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", help="YAML config: connection, datasets, roles.")
    source.add_argument("--demo", action="store_true",
                        help="Serve the bundled Wikipedia edits snapshot with DuckDB.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--role", help="stdio only: role to serve (default: the config's).")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    if args.demo:
        from warehouse_mcp.demo import demo_config_path

        config_path = str(demo_config_path())
    else:
        config_path = args.config
    settings = load_settings(config_path)
    if args.role:
        if args.transport == "http":
            parser.error("--role applies to stdio; over HTTP the bearer key decides the role")
        settings = settings.model_copy(update={"default_role": settings.role(args.role).name})

    try:
        backend = open_backend(settings.connection)
        service = WarehouseService(settings, backend, AuditLog(settings.audit_log),
                                   pii_salt=os.environ.get("WAREHOUSE_MCP_PII_SALT"))
        service.validate_against_warehouse()
    except (BackendError, ConfigMismatch) as exc:
        sys.exit(f"warehouse-mcp: {exc}")

    if args.transport == "http":
        url = f"http://{args.host}:{args.port}"
        build_server(service, http_url=url).run(transport="streamable-http", host=args.host,
                                                port=args.port)
    else:
        build_server(service).run()


if __name__ == "__main__":
    main()
