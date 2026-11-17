import sqlite3
from contextlib import contextmanager
from app.config import DATABASE_PATH

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, accepted INTEGER NOT NULL, quarantined INTEGER NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS fx_acceptances (
        acceptance_id TEXT PRIMARY KEY,
        doc_no TEXT NOT NULL,
        line_no INTEGER NOT NULL DEFAULT 1,
        sku TEXT NOT NULL,
        currency TEXT NOT NULL,
        original_amount TEXT NOT NULL,
        exchange_rate TEXT NOT NULL,
        received_on TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fx_invoices (
        invoice_id TEXT PRIMARY KEY,
        doc_no TEXT NOT NULL,
        line_no INTEGER NOT NULL DEFAULT 1,
        sku TEXT NOT NULL,
        currency TEXT NOT NULL,
        original_amount TEXT NOT NULL,
        exchange_rate TEXT NOT NULL,
        invoiced_on TEXT NOT NULL,
        run_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fx_writeoffs (
        writeoff_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        invoice_id TEXT NOT NULL,
        invoice_doc_no TEXT NOT NULL,
        acceptance_id TEXT NOT NULL,
        acceptance_doc_no TEXT NOT NULL,
        sku TEXT NOT NULL,
        currency TEXT NOT NULL,
        amount TEXT NOT NULL,
        cost_cny TEXT NOT NULL,
        payable_cny TEXT NOT NULL,
        fx_difference_cny TEXT NOT NULL,
        invoice_rate TEXT NOT NULL,
        receipt_rate TEXT NOT NULL,
        seq INTEGER NOT NULL,
        tolerance_residual TEXT NOT NULL DEFAULT '0.00',
        UNIQUE (invoice_id, acceptance_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fx_differences (
        difference_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        invoice_id TEXT NOT NULL UNIQUE,
        invoice_doc_no TEXT NOT NULL,
        line_no INTEGER NOT NULL DEFAULT 0,
        sku TEXT NOT NULL,
        currency TEXT NOT NULL,
        invoiced TEXT NOT NULL,
        cost_cny TEXT NOT NULL,
        payable_cny TEXT NOT NULL,
        fx_difference_cny TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fx_writeoffs_group ON fx_writeoffs (sku, currency)",
    "CREATE INDEX IF NOT EXISTS idx_fx_writeoffs_acceptance ON fx_writeoffs (acceptance_id)",
)

def connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DATABASE_PATH, timeout=10)

def _ensure_columns(db, table: str, columns: dict[str, str]) -> None:
    """Additive migration for databases created by an earlier build."""
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def init_db():
    with connect() as db:
        for statement in SCHEMA:
            db.execute(statement)
        _ensure_columns(db, "fx_writeoffs", {"acceptance_doc_no": "TEXT NOT NULL DEFAULT ''"})
        _ensure_columns(db, "fx_differences", {
            "invoice_doc_no": "TEXT NOT NULL DEFAULT ''",
            "line_no": "INTEGER NOT NULL DEFAULT 0",
        })

@contextmanager
def transaction():
    """Serializable write transaction.

    BEGIN IMMEDIATE takes the write lock before balances are read, so two
    concurrent write-offs cannot both consume the same acceptance balance.
    Any failure rolls every row back together; callers never observe a
    half-posted batch.
    """
    init_db()
    db = sqlite3.connect(DATABASE_PATH, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()


@contextmanager
def snapshot():
    """Read-only connection for page/ledger rendering (never blocks writers)."""
    init_db()
    db = sqlite3.connect(DATABASE_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()

def _insert_run(db, manifest: dict) -> bool:
    before = db.total_changes
    db.execute(
        "INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)",
        (manifest["run_id"], manifest["input_sha256"], manifest["accepted"], manifest["quarantined"]),
    )
    return db.total_changes > before

def record_run(manifest: dict, db=None) -> bool:
    if db is not None:
        return _insert_run(db, manifest)
    init_db()
    with connect() as own:
        inserted = _insert_run(own, manifest)
        own.commit()
        return inserted

def run_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

def fx_writeoff_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM fx_writeoffs").fetchone()[0]

def fx_difference_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM fx_differences").fetchone()[0]
