"""SQLite compatibility shim: lets existing MySQL-style app.py code run on sqlite3.
- connect_db() returns a wrapper whose cursor() accepts dictionary=True/buffered=True kwargs
- execute() auto-converts %s -> ? and rewrites a few MySQL-isms (CONCAT, DATE_FORMAT, NOW(), FOREIGN_KEY_CHECKS, DATE_ADD)
- rows behave like BOTH dicts and tuples (row['col'] and row[0] both work)
- adds datetime parsing so .strftime() keeps working on timestamp columns
"""
import sqlite3
import re
import math
from datetime import datetime, date

DB_PATH = "actbayan.db"

_CONCAT_RE = re.compile(r"CONCAT\s*\((.*?)\)", re.I | re.S)
_DATEFMT_RE = re.compile(r"DATE_FORMAT\s*\(\s*([^,]+?)\s*,\s*'([^']*)'\s*\)", re.I)


def _rewrite_sql(sql: str) -> str:
    if not isinstance(sql, str):
        return sql
    s = sql
    if "FOREIGN_KEY_CHECKS" in s.upper():
        if "FOREIGN_KEY_CHECKS=0" in s.replace(" ", "") or "FOREIGN_KEY_CHECKS = 0" in s:
            return "PRAGMA foreign_keys=OFF"
        return "PRAGMA foreign_keys=ON"
    # CONCAT(a, b, c) -> (a || b || c)
    def _concat(m):
        inner = m.group(1)
        # naive split on commas (safe: usages are simple literals/columns)
        parts = [p.strip() for p in inner.split(",")]
        return "(" + " || ".join(parts) + ")"
    s = _CONCAT_RE.sub(_concat, s)
    # DATE_FORMAT(col, '%b %d, %Y ...') -> strftime('...', col) with mapped specifiers
    def _datefmt(m):
        col, fmt = m.group(1).strip(), m.group(2)
        fmt = (fmt.replace("%b", "%m").replace("%h", "%H").replace("%i", "%M"))
        return f"strftime('{fmt}', {col})"
    s = _DATEFMT_RE.sub(_datefmt, s)
    # NOW() -> CURRENT_TIMESTAMP ; DATE_ADD(NOW(), INTERVAL 5 MINUTE) -> datetime('now','+5 minutes')
    s = re.sub(r"DATE_ADD\s*\(\s*NOW\s*\(\s*\)\s*,\s*INTERVAL\s+(\d+)\s+MINUTE\s*\)", r"datetime('now','+\1 minutes')", s, flags=re.I)
    s = re.sub(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", s, flags=re.I)
    # ---- MySQL date/time functions -> SQLite equivalents ----
    s = re.sub(r"\bCURDATE\s*\(\s*\)", "date('now','localtime')", s, flags=re.I)
    s = re.sub(r"\bCURDATE\b(?!\s*\()", "date('now','localtime')", s, flags=re.I)
    # DATE_SUB(date, INTERVAL n UNIT) -> date(date, '-n unit')
    def _datesub(m):
        base, n, unit = m.group(1).strip(), m.group(2), m.group(3).lower()
        key = unit.rstrip('s') if unit.endswith('s') else unit
        mult = 7 if key == 'week' else (3 if key == 'quarter' else 1)
        sqlite_unit = {'microsecond': 'seconds', 'second': 'seconds', 'minute': 'minutes',
                       'hour': 'hours', 'day': 'days', 'week': 'days', 'month': 'months',
                       'quarter': 'months', 'year': 'years'}.get(key, unit)
        try:
            total = int(n) * mult
        except Exception:
            total = n
        return f"date({base}, '-{total} {sqlite_unit}')"
    s = re.sub(r"DATE_SUB\s*\(\s*(.+?)\s*,\s*INTERVAL\s+(\d+)\s+([A-Za-z]+)\s*\)", _datesub, s, flags=re.I)
    s = re.sub(r"DATE_ADD\s*\(\s*(.+?)\s*,\s*INTERVAL\s+(\d+)\s+([A-Za-z]+)\s*\)",
               lambda m: f"date({m.group(1).strip()}, '+{m.group(2)} {m.group(3).lower()}')", s, flags=re.I)
    # DATEDIFF(a, b) -> whole days difference (MySQL returns integer)
    s = re.sub(r"DATEDIFF\s*\(\s*(.+?)\s*,\s*(.+?)\s*\)", r"CAST((julianday(\1) - julianday(\2)) AS INTEGER)", s, flags=re.I)
    # YEAR(x)/MONTH(x)/DAY(x) -> CAST(strftime(...) AS INTEGER)
    s = re.sub(r"\bYEAR\s*\(\s*(.+?)\s*\)", r"CAST(strftime('%Y', \1) AS INTEGER)", s, flags=re.I)
    s = re.sub(r"\bMONTH\s*\(\s*(.+?)\s*\)", r"CAST(strftime('%m', \1) AS INTEGER)", s, flags=re.I)
    s = re.sub(r"\bDAY\s*\(\s*(.+?)\s*\)", r"CAST(strftime('%d', \1) AS INTEGER)", s, flags=re.I)
    # DAYOFWEEK(x): MySQL Sun=1..Sat=7 ; SQLite %w Sun=0..Sat=6 -> +1
    s = re.sub(r"DAYOFWEEK\s*\(\s*(.+?)\s*\)", r"(CAST(strftime('%w', \1) AS INTEGER) + 1)", s, flags=re.I)
    # YEARWEEK(x,1) ISO equality -> compare via strftime %Y-%W
    s = re.sub(
        r"YEARWEEK\s*\(\s*(.+?)\s*,\s*1\s*\)\s*=\s*YEARWEEK\s*\(\s*date\('now','localtime'\)\s*,\s*1\s*\)",
        r"(strftime('%Y-%W', \1) = strftime('%Y-%W', 'now','localtime'))", s, flags=re.I)
    s = re.sub(r"YEARWEEK\s*\(\s*(.+?)\s*,\s*1\s*\)",
               r"(strftime('%Y', \1) || strftime('%W', \1))", s, flags=re.I)
    s = re.sub(r"YEARWEEK\s*\(\s*(.+?)\s*\)",
               r"(strftime('%Y', \1) || strftime('%W', \1))", s, flags=re.I)
    # CEIL(x) -> handled by CEILING___ UDF registered on each connection
    s = re.sub(r"\bCEIL\s*\(", "CEILING___(", s, flags=re.I)
    s = re.sub(r"\bRAND\s*\(\s*\)", "RANDOM()", s, flags=re.I)
    # UPDATE t1 t2 JOIN ... not supported; handled at call site (ban path rewritten)
    return s


def _parse_dt(v):
    if isinstance(v, str):
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(v, f)
            except Exception:
                continue
    return v


_DT_COLS = {"created_at", "submitted_at", "uploaded_at", "attempt_time", "made_at",
            "verified_at", "lock_duration", "formatted_date", "dt"}


class Row:
    __slots__ = ("_keys", "_vals", "_map")

    def __init__(self, keys, vals):
        self._keys = list(keys)
        self._vals = list(vals)
        self._map = {}
        for k, v in zip(self._keys, self._vals):
            if k in _DT_COLS:
                v = _parse_dt(v)
            self._map[k] = v

    def __getitem__(self, k):
        if isinstance(k, int):
            v = self._vals[k]
            # apply dt parse lazily for positional access
            if self._keys and 0 <= k < len(self._keys) and self._keys[k] in _DT_COLS:
                return _parse_dt(v)
            return v
        return self._map[k]

    def __setitem__(self, k, v):
        if isinstance(k, int):
            self._vals[k] = v
            if self._keys and 0 <= k < len(self._keys):
                self._map[self._keys[k]] = _parse_dt(v) if self._keys[k] in _DT_COLS else v
        else:
            if k not in self._map:
                self._keys.append(k)
                self._vals.append(v)
            else:
                try:
                    self._vals[self._keys.index(k)] = v
                except ValueError:
                    pass
            self._map[k] = v

    def __contains__(self, k):
        return k in self._map

    def get(self, k, d=None):
        return self._map.get(k, d)

    def keys(self):
        return self._keys

    def items(self):
        return self._map.items()

    def __iter__(self):
        # dict-like iteration would break tuple-unpack `a, b = row`; default to values
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def to_dict(self):
        """Plain JSON-safe dict (datetime -> string, bytes -> str)."""
        out = {}
        for k, v in self._map.items():
            if isinstance(v, datetime):
                out[k] = v.strftime('%Y-%m-%d %H:%M:%S')
            elif isinstance(v, (date,)):
                out[k] = v.isoformat()
            elif isinstance(v, (bytes, bytearray)):
                try:
                    out[k] = bytes(v).decode('utf-8', errors='ignore')
                except Exception:
                    out[k] = ''
            elif isinstance(v, float) and (v != v or v in (float('inf'), float('-inf'))):
                out[k] = None
            else:
                out[k] = v
        return out


class Cursor:
    def __init__(self, cur):
        self._c = cur
        self.lastrowid = None
        self.description = None

    def execute(self, sql, params=None):
        sql2 = _rewrite_sql(sql).replace("%s", "?")
        if params is None:
            r = self._c.execute(sql2)
        else:
            if not isinstance(params, (list, tuple)):
                params = (params,)
            r = self._c.execute(sql2, tuple(params))
        self.lastrowid = self._c.lastrowid
        self.description = self._c.description
        return r

    def executemany(self, sql, seq):
        sql2 = _rewrite_sql(sql).replace("%s", "?")
        r = self._c.executemany(sql2, seq)
        self.lastrowid = self._c.lastrowid
        return r

    def _wrap(self, row):
        if row is None:
            return None
        keys = [d[0] for d in self._c.description] if self._c.description else []
        if isinstance(row, sqlite3.Row):
            vals = [row[k] for k in keys]
        else:
            vals = list(row)
            if keys and len(vals) != len(keys):
                keys = [f"c{i}" for i in range(len(vals))]
        return Row(keys, vals)

    def fetchone(self):
        return self._wrap(self._c.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._c.fetchall()]

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass


def _ensure_sqlite_funcs(con):
    """Register CEIL + helpers missing from SQLite so MySQL queries keep working."""
    try:
        con.create_function("CEILING___", 1, lambda x: (math.ceil(float(x)) if x is not None else None))
    except Exception:
        pass


class Connection:
    def __init__(self, path=DB_PATH):
        self._con = sqlite3.connect(path)
        self._con.row_factory = None
        _ensure_sqlite_funcs(self._con)
        try:
            self._con.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass

    def cursor(self, *a, **kw):
        return Cursor(self._con.cursor())

    def commit(self):
        return self._con.commit()

    def rollback(self):
        return self._con.rollback()

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass

    def is_connected(self):
        return True


def connect_db():
    return Connection(DB_PATH)
