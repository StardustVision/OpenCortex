# SPDX-License-Identifier: Apache-2.0
"""
CLI tool for managing OpenCortex JWT tokens.

Usage::

    opencortex-token generate   # Interactive — prompts for tenant_id / user_id
    opencortex-token list       # Show all issued tokens
    opencortex-token revoke <prefix>  # Revoke a token by prefix
"""

import argparse
import sys

from opencortex.auth.token import (
    ensure_secret,
    generate_token,
    load_token_records,
    revoke_token,
    save_token_record,
)
from opencortex.config import get_config


def _get_data_root() -> str:
    config = get_config()
    return config.data_root


def cmd_generate(_args: argparse.Namespace) -> None:
    """Interactively generate a new JWT token."""
    data_root = _get_data_root()
    secret = ensure_secret(data_root)

    tenant_id = input("Tenant ID: ").strip()
    if not tenant_id:
        print("Error: tenant_id cannot be empty", file=sys.stderr)
        sys.exit(1)

    user_id = input("User ID: ").strip()
    if not user_id:
        print("Error: user_id cannot be empty", file=sys.stderr)
        sys.exit(1)

    token = generate_token(tenant_id, user_id, secret)
    save_token_record(data_root, token, tenant_id, user_id)

    print(f"\nToken generated for {tenant_id}/{user_id}:\n")
    print(token)
    print(f"\nRecorded in {data_root}/tokens.json")


def cmd_list(_args: argparse.Namespace) -> None:
    """List all issued tokens."""
    data_root = _get_data_root()
    records = load_token_records(data_root)

    if not records:
        print("No tokens issued yet.")
        return

    print(f"{'Tenant':<16} {'User':<16} {'Created':<28} {'Token (prefix)':<20}")
    print("-" * 80)
    for rec in records:
        token_prefix = rec["token"][:16] + "..."
        print(
            f"{rec['tenant_id']:<16} "
            f"{rec['user_id']:<16} "
            f"{rec.get('created_at', 'N/A'):<28} "
            f"{token_prefix:<20}"
        )


def cmd_revoke(args: argparse.Namespace) -> None:
    """Revoke a token by prefix."""
    data_root = _get_data_root()
    removed = revoke_token(data_root, args.prefix)

    if removed:
        print(
            f"Revoked token for {removed['tenant_id']}/{removed['user_id']} "
            f"(created {removed.get('created_at', 'N/A')})"
        )
    else:
        print(f"No token found matching prefix: {args.prefix}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="opencortex-token",
        description="Manage OpenCortex JWT tokens",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("generate", help="Generate a new token (interactive)")
    sub.add_parser("list", help="List all issued tokens")

    revoke_p = sub.add_parser("revoke", help="Revoke a token by prefix")
    revoke_p.add_argument("prefix", help="Token prefix to match")

    args = parser.parse_args()

    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "revoke":
        cmd_revoke(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
