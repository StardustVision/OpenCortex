# SPDX-License-Identifier: Apache-2.0
"""CLI entry point for the opencortex_app FastAPI application."""

from __future__ import annotations

import argparse


def main() -> None:
    """Parse CLI arguments and run the opencortex_app server."""
    parser = argparse.ArgumentParser(
        prog="opencortex-app",
        description="OpenCortex write-path FastAPI app",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8921, help="Bind port")
    parser.add_argument("--config", default=None, help="Path to config file")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=False,
        help="Enable auto-reload for development",
    )
    args = parser.parse_args()

    from opencortex_app.logging import configure_logging

    configure_logging(args.log_level)

    import uvicorn

    uvicorn.run(
        "opencortex_app.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
