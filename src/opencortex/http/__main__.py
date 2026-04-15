# SPDX-License-Identifier: Apache-2.0
"""CLI entry point for the OpenCortex HTTP Server.

Usage::

    uv run opencortex-server --host 127.0.0.1 --port 8921 --config server.json
"""

import argparse
import logging


def main() -> None:
    """Parse CLI arguments and start the HTTP server."""
    parser = argparse.ArgumentParser(
        prog="opencortex.http",
        description="OpenCortex HTTP Server (FastAPI)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8921,
        help="Bind port (default: 8921)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config file (default: auto-discover server.json)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=False,
        help="Enable auto-reload for development",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Initialize config before importing the app (so lifespan sees it)
    from opencortex.config import CortexConfig, init_config

    if not args.config:
        # Auto-create $HOME/.opencortex/server.json if it doesn't exist
        CortexConfig.ensure_default_config()
    init_config(path=args.config)

    import uvicorn

    uvicorn.run(
        "opencortex.http.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
