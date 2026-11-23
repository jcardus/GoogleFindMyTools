"""Direct PostgreSQL access for the Google worker."""

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def database_url() -> str:
    value = os.getenv("SUPABASE_DB_URL")
    if not value:
        raise RuntimeError("SUPABASE_DB_URL is required")
    return value


@contextmanager
def connection():
    with psycopg.connect(database_url(), connect_timeout=10, row_factory=dict_row) as conn:
        yield conn


class _Query:
    def __init__(self, db, table, operation="select"):
        self.db, self.table_name, self.operation = db, table, operation
        self.columns, self.filters, self.ordering = "*", [], []
        self.offset, self.limit = 0, None
        self.values, self.conflict, self.ignore_duplicates = None, None, False

    def select(self, columns="*", **_kwargs):
        self.columns = columns
        return self

    def eq(self, column, value):
        self.filters.append((column, "IS NOT DISTINCT FROM", value))
        return self

    def filter(self, column, operator, value):
        if operator == "not.is":
            self.filters.append((column, "IS NOT", None))
        elif operator == "neq":
            self.filters.append((column, "<>", value))
        elif operator == "eq":
            self.filters.append((column, "IS NOT DISTINCT FROM", value))
        else:
            raise ValueError(f"unsupported database filter: {operator}")
        return self

    def gte(self, column, value):
        self.filters.append((column, ">=", value))
        return self

    def lte(self, column, value):
        self.filters.append((column, "<=", value))
        return self

    def or_(self, expression):
        clauses = []
        for part in expression.split(","):
            column, op, value = part.split(".", 2)
            if op == "is" and value == "null":
                clauses.append(f'"{column}" IS NULL')
            else:
                raise ValueError(f"unsupported OR filter: {part}")
        self.filters.append(("__raw_or__", "", " OR ".join(clauses)))
        return self

    def order(self, column):
        self.ordering.append(column)
        return self

    def range(self, start, end):
        self.offset, self.limit = start, end - start + 1
        return self

    def update(self, values):
        self.operation, self.values = "update", values
        return self

    def insert(self, values):
        self.operation, self.values = "insert", values
        return self

    def upsert(self, values, on_conflict=None, ignore_duplicates=False):
        self.operation, self.values = "upsert", values
        self.conflict, self.ignore_duplicates = on_conflict, ignore_duplicates
        return self

    def execute(self):
        return self.db._execute(self)


class _Response:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class Database:
    def __init__(self):
        self._conn = psycopg.connect(database_url(), connect_timeout=10, row_factory=dict_row)

    def table(self, table):
        return _Query(self, table)

    def _where(self, query):
        if not query.filters:
            return "", []
        clauses, params = [], []
        for column, operator, value in query.filters:
            if column == "__raw_or__":
                clauses.append(f"({value})")
                continue
            clauses.append(f'"{column}" {operator} %s' if value is not None else f'"{column}" {operator} NULL')
            if value is not None:
                params.append(value)
        return " WHERE " + " AND ".join(clauses), params

    def _execute(self, query):
        where, params = self._where(query)
        cur = self._conn.cursor()
        try:
            if query.operation == "select":
                sql = f'SELECT {query.columns} FROM "{query.table_name}"{where}'
                if query.ordering:
                    sql += " ORDER BY " + ", ".join(f'"{c}"' for c in query.ordering)
                if query.limit is not None:
                    sql += " OFFSET %s LIMIT %s"
                    params += [query.offset, query.limit]
                cur.execute(sql, params)
                rows = cur.fetchall()
                return _Response(rows, len(rows))
            if query.operation in ("insert", "update"):
                if query.operation == "insert":
                    columns = list(query.values)
                    names = ", ".join(f'"{c}"' for c in columns)
                    placeholders = ", ".join(["%s"] * len(columns))
                    cur.execute(f'INSERT INTO "{query.table_name}" ({names}) VALUES ({placeholders}) RETURNING *', list(query.values.values()))
                    rows = cur.fetchall()
                    self._conn.commit()
                    return _Response(rows)
                assignments = ", ".join(f'"{key}" = %s' for key in query.values)
                cur.execute(f'UPDATE "{query.table_name}" SET {assignments}{where} RETURNING *', list(query.values.values()) + params)
                rows = cur.fetchall()
            elif query.operation == "upsert":
                columns = list(query.values)
                names = ", ".join(f'"{c}"' for c in columns)
                placeholders = ", ".join(["%s"] * len(columns))
                conflict = ", ".join(f'"{c.strip()}"' for c in (query.conflict or "").split(",") if c.strip())
                action = "DO NOTHING" if query.ignore_duplicates else "DO UPDATE SET " + ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in columns if c not in (query.conflict or "").split(","))
                cur.execute(f'INSERT INTO "{query.table_name}" ({names}) VALUES ({placeholders}) ON CONFLICT ({conflict}) {action} RETURNING *', list(query.values.values()))
                rows = cur.fetchall()
            else:
                raise ValueError(f"unsupported operation: {query.operation}")
            self._conn.commit()
            return _Response(rows)
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self):
        self._conn.close()

