#!/usr/bin/env python3
"""Smoke-test the direct Supabase PostgreSQL connection."""
import os
import sys

import psycopg


def main() -> int:
    dsn = os.getenv("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("SUPABASE_DB_URL is required")
    with psycopg.connect(dsn, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select 1")
            if cursor.fetchone() != (1,):
                raise SystemExit("basic query returned an unexpected result")
            for table in ("google_accounts", "hybrid_tags", "positions"):
                cursor.execute("select to_regclass(%s)", (table,))
                if cursor.fetchone()[0] != table:
                    raise SystemExit(f"required table is missing: {table}")
    print("Direct Supabase PostgreSQL connection and required tables are available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
