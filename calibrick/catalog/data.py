"""Rebrickable catalog data.

Rebrickable asks tools to use its public CSV dumps (not the API) for bulk catalog data:
https://rebrickable.com/downloads/ . ``ensure_data()`` fetches those dumps once, and
everything else queries a local SQLite file built from them, so set lookups are instant
and work offline.

Two on-disk layouts are accepted in the data directory (``config.rebrickable_dir()``):

* ``<table>.csv`` files as downloaded  -> ``rebrickable.sqlite`` is built next to them
* ``rb.db`` from the open-source https://github.com/ojuuji/rb.db project (same tables
  and column names); used as-is when it is newer than any CSVs present.
"""
from __future__ import annotations

import csv
import datetime as _dt
import gzip
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from .. import config

BASE_URL = "https://cdn.rebrickable.com/media/downloads/{table}.csv.gz"
USER_AGENT = "calibrick/0.1 (personal hobby tool; uses the public CSV dumps, not the API)"

# every table the dumps offer that we use; the first group must be present
REQUIRED = (
    "colors",
    "part_categories",
    "parts",
    "part_relationships",
    "sets",
    "inventories",
    "inventory_parts",
)
OPTIONAL = ("themes", "elements", "minifigs", "inventory_minifigs", "inventory_sets")  # inventory_sets: sub-sets
TABLES = REQUIRED + OPTIONAL

# column layout of the dumps (used to create empty optional tables and to type columns)
COLUMNS: dict[str, tuple[str, ...]] = {
    "themes": ("id", "name", "parent_id"),
    "colors": ("id", "name", "rgb", "is_trans", "num_parts", "num_sets", "y1", "y2"),
    "part_categories": ("id", "name"),
    "parts": ("part_num", "name", "part_cat_id", "part_material"),
    "part_relationships": ("rel_type", "child_part_num", "parent_part_num"),
    "elements": ("element_id", "part_num", "color_id", "design_id"),
    "minifigs": ("fig_num", "name", "num_parts", "img_url"),
    "sets": ("set_num", "name", "year", "theme_id", "num_parts", "img_url"),
    "inventories": ("id", "version", "set_num"),
    "inventory_parts": ("inventory_id", "part_num", "color_id", "quantity", "is_spare", "img_url"),
    "inventory_minifigs": ("inventory_id", "fig_num", "quantity"),
    "inventory_sets": ("inventory_id", "set_num", "quantity"),
}
_INT_COLS = {
    "id", "parent_id", "is_trans", "num_parts", "num_sets", "y1", "y2", "part_cat_id",
    "element_id", "color_id", "design_id", "year", "theme_id", "version", "inventory_id",
    "quantity", "is_spare",
}
_INDEXES = (
    "CREATE INDEX ix_sets_num ON sets(set_num COLLATE NOCASE)",
    "CREATE INDEX ix_inv_set ON inventories(set_num, version)",
    "CREATE INDEX ix_ip_inv ON inventory_parts(inventory_id)",
    "CREATE INDEX ix_im_inv ON inventory_minifigs(inventory_id)",
    "CREATE INDEX ix_is_inv ON inventory_sets(inventory_id)",
    "CREATE INDEX ix_parts_num ON parts(part_num)",
    "CREATE INDEX ix_rel_child ON part_relationships(child_part_num)",
    "CREATE INDEX ix_rel_parent ON part_relationships(parent_part_num)",
    "CREATE INDEX ix_el_part ON elements(part_num, color_id)",
    "CREATE INDEX ix_colors_id ON colors(id)",
    "CREATE INDEX ix_cat_id ON part_categories(id)",
    "CREATE INDEX ix_fig_num ON minifigs(fig_num)",
)

SQLITE_NAME = "rebrickable.sqlite"
RBDB_NAME = "rb.db"


class DataError(RuntimeError):
    """Catalog data is missing or could not be downloaded."""


# ----------------------------------------------------------------------------- download
def ensure_data(dest: Path | None = None, *, refresh: bool = False, log=None) -> Path:
    """Make sure the Rebrickable catalog is available locally; return its directory.

    Downloads the CSV dumps that are missing (all of them with ``refresh=True``) and
    (re)builds the SQLite file. Does nothing if a usable ``rb.db`` or CSV set is already
    there and ``refresh`` is False.
    """
    d = Path(dest) if dest else config.rebrickable_dir()
    d.mkdir(parents=True, exist_ok=True)
    say = log or (lambda *_: None)

    have_rbdb = (d / RBDB_NAME).exists()
    todo = [t for t in TABLES if refresh or not (d / f"{t}.csv").exists()]
    if have_rbdb and not refresh:
        todo = []
    if todo:
        try:
            import requests
        except ImportError as e:  # pragma: no cover - requests is a declared dependency
            raise DataError("the 'requests' package is needed to download catalog data") from e
        sess = requests.Session()
        sess.headers["User-Agent"] = USER_AGENT
        got: dict[str, int] = {}
        for i, table in enumerate(todo):
            url = BASE_URL.format(table=table)
            say(f"downloading {url}")
            gz_tmp = d / f".{table}.csv.gz.part"
            csv_tmp = d / f".{table}.csv.part"
            try:
                with sess.get(url, stream=True, timeout=120) as r:
                    if r.status_code == 404 and table in OPTIONAL:
                        continue
                    r.raise_for_status()
                    with open(gz_tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            f.write(chunk)
                with gzip.open(gz_tmp, "rb") as src, open(csv_tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except (requests.RequestException, OSError, EOFError) as e:
                for p in (gz_tmp, csv_tmp):
                    p.unlink(missing_ok=True)
                raise DataError(
                    f"could not download {url} ({e}). Check your internet connection, or "
                    f"download the CSV files by hand from https://rebrickable.com/downloads/ "
                    f"and unzip them into {d}"
                ) from e
            os.replace(csv_tmp, d / f"{table}.csv")
            gz_tmp.unlink(missing_ok=True)
            got[table] = (d / f"{table}.csv").stat().st_size
            if i + 1 < len(todo):
                time.sleep(0.4)  # be polite to the CDN
        meta = {"tables": {}}
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            pass
        meta["source"] = "https://rebrickable.com/downloads/"
        meta["downloaded_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        meta.setdefault("tables", {}).update(got)
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
    _close_cached(d)
    open_db(d)  # builds the SQLite file if needed, raises DataError if still unusable
    return d


# ----------------------------------------------------------------------------- sqlite
def _to_db(col: str, value: str):
    if value == "":
        return None
    if col.startswith("is_"):
        return 1 if value.strip().lower() in ("t", "true", "1", "y", "yes") else 0
    if col in _INT_COLS:
        try:
            return int(value)
        except ValueError:
            return value
    return value


def build_sqlite(csv_dir: Path, db_path: Path | None = None) -> Path:
    """Build ``rebrickable.sqlite`` from the CSV dumps in ``csv_dir``."""
    csv_dir = Path(csv_dir)
    db_path = Path(db_path) if db_path else csv_dir / SQLITE_NAME
    missing = [t for t in REQUIRED if not (csv_dir / f"{t}.csv").exists()]
    if missing:
        raise DataError(f"missing Rebrickable CSV files in {csv_dir}: {', '.join(missing)}")
    tmp = db_path.with_name(db_path.name + ".part")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        for table in TABLES:
            path = csv_dir / f"{table}.csv"
            if not path.exists():
                _create_table(con, table, COLUMNS[table])
                continue
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                cols = tuple(h.strip() for h in header) if header else COLUMNS[table]
                _create_table(con, table, cols)
                ph = ", ".join("?" for _ in cols)
                n = len(cols)
                con.executemany(
                    f'INSERT INTO "{table}" VALUES ({ph})',
                    ([_to_db(c, v) for c, v in zip(cols, (row + [""] * n)[:n])] for row in reader if row),
                )
        for stmt in _INDEXES:
            con.execute(stmt)
        stamp = _csv_timestamp(csv_dir)
        con.execute("CREATE TABLE calibrick_meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO calibrick_meta VALUES ('data_timestamp', ?)", (stamp,))
        con.commit()
    finally:
        con.close()
    os.replace(tmp, db_path)
    return db_path


def _create_table(con: sqlite3.Connection, table: str, cols: tuple[str, ...]) -> None:
    decl = ", ".join(f'"{c}" {"INTEGER" if c in _INT_COLS else "TEXT"}' for c in cols)
    con.execute(f'CREATE TABLE "{table}" ({decl})')


def _csv_timestamp(csv_dir: Path) -> str:
    meta = csv_dir / "meta.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text())["downloaded_at"]
        except (ValueError, KeyError, OSError):
            pass
    mt = [(csv_dir / f"{t}.csv").stat().st_mtime for t in REQUIRED if (csv_dir / f"{t}.csv").exists()]
    if not mt:
        return ""
    return _dt.datetime.fromtimestamp(max(mt), _dt.timezone.utc).isoformat(timespec="seconds")


def _pick_db(d: Path) -> Path:
    """Choose (and build if necessary) the SQLite file to use in directory ``d``."""
    rbdb = d / RBDB_NAME
    csvs = [d / f"{t}.csv" for t in TABLES if (d / f"{t}.csv").exists()]
    have_csv = all((d / f"{t}.csv").exists() for t in REQUIRED)
    newest_csv = max((c.stat().st_mtime for c in csvs), default=0.0)
    if rbdb.exists() and not (have_csv and newest_csv > rbdb.stat().st_mtime):
        return rbdb
    built = d / SQLITE_NAME
    if have_csv:
        if not built.exists() or built.stat().st_mtime < newest_csv:
            build_sqlite(d, built)
        return built
    if built.exists():
        return built
    raise DataError(
        f"no Rebrickable catalog data in {d}. Run `calibrick data update` (needs internet) "
        f"or unzip the CSV files from https://rebrickable.com/downloads/ into that folder."
    )


_CONNS: dict[str, sqlite3.Connection] = {}


def _close_cached(d: Path) -> None:
    for key in [k for k in _CONNS if k.startswith(str(Path(d).resolve()))]:
        try:
            _CONNS.pop(key).close()
        except sqlite3.Error:
            pass


def open_db(dest: Path | None = None) -> sqlite3.Connection:
    """Read-only connection (rows are ``sqlite3.Row``) to the catalog in ``dest``."""
    d = (Path(dest) if dest else config.rebrickable_dir()).resolve()
    path = _pick_db(d)
    key = f"{path}|{path.stat().st_mtime_ns}"  # a rebuilt file gets a fresh connection
    con = _CONNS.get(key)
    if con is None:
        for old in [k for k in _CONNS if k.split("|")[0] == str(path)]:
            _CONNS.pop(old).close()
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        _CONNS[key] = con
    return con


def data_info(dest: Path | None = None) -> dict:
    """Where the catalog comes from and how fresh it is."""
    d = (Path(dest) if dest else config.rebrickable_dir()).resolve()
    path = _pick_db(d)
    con = open_db(d)
    stamp = ""
    if path.name == RBDB_NAME:
        try:
            row = con.execute("SELECT value FROM rb_db_lov WHERE key='data_timestamp'").fetchone()
            if row:
                stamp = _dt.datetime.fromtimestamp(int(row[0]), _dt.timezone.utc).isoformat(timespec="seconds")
        except (sqlite3.Error, ValueError):
            pass
        if not stamp:
            stamp = _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc).isoformat(timespec="seconds")
    else:
        try:
            row = con.execute("SELECT value FROM calibrick_meta WHERE key='data_timestamp'").fetchone()
            stamp = row[0] if row else ""
        except sqlite3.Error:
            stamp = ""
    n_sets = con.execute("SELECT count(*) FROM sets").fetchone()[0]
    return {
        "dir": str(d),
        "file": path.name,
        "source": "rb.db" if path.name == RBDB_NAME else "rebrickable csv",
        "timestamp": stamp,
        "sets": n_sets,
    }


def has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone() is not None
