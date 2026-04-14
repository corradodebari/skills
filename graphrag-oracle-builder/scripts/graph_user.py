#!/usr/bin/env python3
"""
graph_user.py — GraphRAG user phase

This script manages only Oracle user validation/creation and stops before chunking.
"""

from __future__ import annotations

import argparse
import sys

import oracledb

from graphrag_builder import create_user_interactively


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GraphRAG user phase: check/create Oracle user and stop before chunking"
    )
    parser.add_argument("--db-user", required=True, help="Oracle username")
    parser.add_argument("--db-password", required=True, help="Oracle password")
    parser.add_argument("--db-dsn", required=True, help="Oracle DSN: host:port/service_name")
    parser.add_argument(
        "--create-user",
        action="store_true",
        help="Interactively create a new GraphRAG Oracle user (requires DBA privileges)",
    )
    args = parser.parse_args()

    print("\n[0/4] Connecting to Oracle DB …")
    conn = oracledb.connect(user=args.db_user, password=args.db_password, dsn=args.db_dsn)
    print(f"  Connected  (Oracle {conn.version}) as {args.db_user.upper()}")

    active_user = args.db_user
    if args.create_user:
        new_user, new_pass = create_user_interactively(conn)
        conn.close()
        print(f"  Reconnecting as {new_user} …")
        conn = oracledb.connect(user=new_user, password=new_pass, dsn=args.db_dsn)
        print(f"  Connected as {new_user.upper()}")
        active_user = new_user
    else:
        print(f"  Using existing user {active_user.upper()}")

    conn.close()
    print(f"  User phase complete for {active_user.upper()}.")
    


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)
