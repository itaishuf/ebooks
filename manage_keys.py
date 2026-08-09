#!/usr/bin/env python3
"""CLI tool for managing ebookarr API keys.

Usage:
    uv run manage_keys.py list
    uv run manage_keys.py add --label "Dad" --email dad@kindle.com
    uv run manage_keys.py add --label "Yossi"
    uv run manage_keys.py revoke <key_id>
    uv run manage_keys.py quota <key_id> [--set 20]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from api_keys import ApiKeyStore

DEFAULT_KEYS_FILE = "api_keys.json"


def get_store(args: argparse.Namespace) -> ApiKeyStore:
    file_path = args.file or DEFAULT_KEYS_FILE
    return ApiKeyStore(file_path)


def cmd_list(args: argparse.Namespace) -> None:
    store = get_store(args)
    keys = store.list_keys()
    if not keys:
        print("No API keys found.")
        return
    print(f"{'KEY ID':<12} {'LABEL':<20} {'EMAILS':<30} {'QUOTA':<8} {'ENABLED'}")
    print("-" * 80)
    for key in keys:
        emails = ", ".join(key.allowed_emails) if key.allowed_emails else "(any)"
        print(f"{key.key_id:<12} {key.label:<20} {emails:<30} {key.daily_quota:<8} {key.enabled}")


def cmd_add(args: argparse.Namespace) -> None:
    store = get_store(args)
    allowed_emails = [e.strip() for e in args.email.split(",")] if args.email else []
    raw_key, record = store.add_key(
        label=args.label,
        allowed_emails=allowed_emails,
        daily_quota=args.quota,
    )
    print(f"Key created successfully!")
    print(f"  Key ID:  {record.key_id}")
    print(f"  Label:   {record.label}")
    print(f"  Quota:   {record.daily_quota}/day")
    if record.allowed_emails:
        print(f"  Emails:  {', '.join(record.allowed_emails)}")
    print()
    print(f"  Raw key (save this now, it won't be shown again):")
    print(f"  {raw_key}")


def cmd_revoke(args: argparse.Namespace) -> None:
    store = get_store(args)
    if store.revoke_key(args.key_id):
        print(f"Key {args.key_id} revoked.")
    else:
        print(f"Key {args.key_id} not found.")
        sys.exit(1)


def cmd_quota(args: argparse.Namespace) -> None:
    store = get_store(args)
    record = store.get_key(args.key_id)
    if record is None:
        print(f"Key {args.key_id} not found.")
        sys.exit(1)
    if args.set is not None:
        store.update_quota(args.key_id, args.set)
        print(f"Quota for key {args.key_id} updated to {args.set}/day.")
    else:
        print(f"Key {args.key_id}: {record.daily_quota}/day")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage ebookarr API keys")
    parser.add_argument("-f", "--file", default=None, help="Path to api_keys.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List all API keys")

    add_parser = subparsers.add_parser("add", help="Add a new API key")
    add_parser.add_argument("--label", required=True, help="Label for the key (who it belongs to)")
    add_parser.add_argument("--email", default="", help="Comma-separated allowed Kindle emails (empty = any)")
    add_parser.add_argument("--quota", type=int, default=15, help="Daily download quota (default: 15)")

    revoke_parser = subparsers.add_parser("revoke", help="Revoke an API key")
    revoke_parser.add_argument("key_id", help="Key ID to revoke")

    quota_parser = subparsers.add_parser("quota", help="View or update daily quota")
    quota_parser.add_argument("key_id", help="Key ID")
    quota_parser.add_argument("--set", type=int, default=None, help="Set new daily quota")

    args = parser.parse_args()
    {"list": cmd_list, "add": cmd_add, "revoke": cmd_revoke, "quota": cmd_quota}[args.command](args)


if __name__ == "__main__":
    main()
