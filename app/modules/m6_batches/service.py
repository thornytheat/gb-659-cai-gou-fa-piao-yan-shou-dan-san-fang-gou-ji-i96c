from decimal import Decimal

from app.errors import DomainError
from app.manifest import build_manifest
from app import store
from . import fx

SAMPLE_ACCEPTANCES = [
    {"acceptance_id": "GR-100#1", "doc_no": "GR-100", "line_no": 1, "sku": "SKU-A",
     "currency": "USD", "original_amount": "1000.00", "exchange_rate": "7.1000", "received_on": "2026-01-05"},
    {"acceptance_id": "GR-101#1", "doc_no": "GR-101", "line_no": 1, "sku": "SKU-A",
     "currency": "USD", "original_amount": "500.00", "exchange_rate": "7.2000", "received_on": "2026-01-20"},
    {"acceptance_id": "GR-102#1", "doc_no": "GR-102", "line_no": 1, "sku": "SKU-C",
     "currency": "CNY", "original_amount": "226.00", "exchange_rate": "1", "received_on": "2026-01-22"},
]
SAMPLE_INVOICES = [
    {"invoice_id": "IV-200#1", "doc_no": "IV-200", "line_no": 1, "sku": "SKU-A",
     "currency": "USD", "original_amount": "1200.00", "exchange_rate": "7.3000", "invoiced_on": "2026-02-10"},
]


# ---------------------------------------------------------------------------
# Ledger reads (Decimal only; never aggregate money in SQLite floats)
# ---------------------------------------------------------------------------

def _sum_by_acceptance(db) -> dict[str, dict]:
    totals: dict[str, dict] = {}
    for row in db.execute(
        "SELECT acceptance_id, amount, cost_cny, payable_cny, fx_difference_cny FROM fx_writeoffs"
    ):
        bucket = totals.setdefault(row["acceptance_id"], {
            "amount": Decimal("0"), "cost": Decimal("0"),
            "payable": Decimal("0"), "fx": Decimal("0"), "count": 0,
        })
        bucket["amount"] += Decimal(row["amount"])
        bucket["cost"] += Decimal(row["cost_cny"])
        bucket["payable"] += Decimal(row["payable_cny"])
        bucket["fx"] += Decimal(row["fx_difference_cny"])
        bucket["count"] += 1
    return totals


def load_acceptances(db) -> list[dict]:
    written = _sum_by_acceptance(db)
    result = []
    for row in db.execute("SELECT * FROM fx_acceptances ORDER BY received_on, acceptance_id"):
        opening_cost = fx.acceptance_cost(Decimal(row["original_amount"]), Decimal(row["exchange_rate"]))
        used = written.get(row["acceptance_id"], {"amount": Decimal("0"), "cost": Decimal("0")})
        result.append({
            "acceptance_id": row["acceptance_id"],
            "doc_no": row["doc_no"],
            "line_no": row["line_no"],
            "sku": row["sku"],
            "currency": row["currency"],
            "original_amount": Decimal(row["original_amount"]),
            "exchange_rate": Decimal(row["exchange_rate"]),
            "received_on": fx.business_date(row["received_on"]),
            "version": row["version"],
            "cost_cny": opening_cost,
            "written_off": used["amount"],
            "open_amount": Decimal(row["original_amount"]) - used["amount"],
            "open_cost": opening_cost - used["cost"],
        })
    return result


def load_groups(db) -> list[dict]:
    buckets: dict[tuple[str, str], dict] = {}
    for row in load_acceptances(db):
        bucket = buckets.setdefault((row["sku"], row["currency"]), {
            "sku": row["sku"], "currency": row["currency"],
            "received": Decimal("0"), "written_off": Decimal("0"),
            "cost_cny": Decimal("0"), "open_cost": Decimal("0"),
            "payable_cny": Decimal("0"), "fx_difference_cny": Decimal("0"),
        })
        bucket["received"] += row["original_amount"]
        bucket["written_off"] += row["written_off"]
        bucket["cost_cny"] += row["cost_cny"]
        bucket["open_cost"] += row["open_cost"]
    for row in db.execute(
        "SELECT acceptance_id, sku, currency, payable_cny, fx_difference_cny FROM fx_writeoffs"
    ):
        bucket = buckets.get((row["sku"], row["currency"]))
        if bucket is None:
            continue
        bucket["payable_cny"] += Decimal(row["payable_cny"])
        bucket["fx_difference_cny"] += Decimal(row["fx_difference_cny"])
    groups = []
    for group in buckets.values():
        group["uninvoiced"] = group["received"] - group["written_off"]
        group["uninvoiced_cost_cny"] = group["open_cost"]
        groups.append(group)
    groups.sort(key=lambda item: (item["currency"], item["sku"]))
    return groups


def load_invoices(db) -> list[dict]:
    invoices = []
    for row in db.execute("SELECT * FROM fx_invoices ORDER BY invoiced_on, invoice_id"):
        allocations = []
        for item in db.execute(
            "SELECT * FROM fx_writeoffs WHERE invoice_id = ? ORDER BY seq", (row["invoice_id"],)
        ):
            allocations.append({
                "acceptance_id": item["acceptance_id"],
                "acceptance_doc_no": item["acceptance_doc_no"],
                "sequence": item["seq"],
                "amount": Decimal(item["amount"]),
                "receipt_rate": Decimal(item["receipt_rate"]),
                "invoice_rate": Decimal(item["invoice_rate"]),
                "cost_cny": Decimal(item["cost_cny"]),
                "payable_cny": Decimal(item["payable_cny"]),
                "fx_difference_cny": Decimal(item["fx_difference_cny"]),
            })
        original = Decimal(row["original_amount"])
        payable_total = fx.invoice_payable(original, Decimal(row["exchange_rate"]))
        cost_total = sum((item["cost_cny"] for item in allocations), Decimal("0"))
        invoices.append({
            "invoice_id": row["invoice_id"],
            "doc_no": row["doc_no"],
            "line_no": row["line_no"],
            "sku": row["sku"],
            "currency": row["currency"],
            "original_amount": original,
            "exchange_rate": Decimal(row["exchange_rate"]),
            "invoiced_on": fx.business_date(row["invoiced_on"]),
            "run_id": row["run_id"],
            "payable_cny": payable_total,
            "cost_cny": fx.money(cost_total),
            "fx_difference_cny": payable_total - fx.money(cost_total),
            "status": "WRITTEN_OFF",
            "allocations": allocations,
        })
    return invoices


def load_differences(db) -> list[dict]:
    return [{
        "business_key": f"{row['sku']}|{row['currency']}",
        "doc_no": row["invoice_doc_no"],
        "line_no": row["line_no"],
        "reason_code": "FX_DIFFERENCE",
        "reason": fx.reason("FX_DIFFERENCE"),
        "currency": row["currency"],
        "original_amount": row["invoiced"],
        "amount_cny": row["fx_difference_cny"],
        "cost_cny": row["cost_cny"],
        "payable_cny": row["payable_cny"],
        "fx_difference_cny": row["fx_difference_cny"],
    } for row in db.execute("SELECT * FROM fx_differences ORDER BY invoice_id")]


def _difference_rows(db) -> list[dict]:
    """One evidence list: FX rows already booked plus currently uninvoiced balances."""
    rows = load_differences(db)
    for group in load_groups(db):
        if group["uninvoiced"] == 0:
            continue
        rows.append({
            "business_key": f"{group['sku']}|{group['currency']}",
            "doc_no": "",
            "line_no": 0,
            "reason_code": "UNINVOICED",
            "reason": fx.reason("UNINVOICED"),
            "currency": group["currency"],
            "original_amount": fx.amount_text(group["uninvoiced"]),
            "amount_cny": fx.plain(fx.money(group["uninvoiced_cost_cny"])),
            "cost_cny": fx.plain(fx.money(group["uninvoiced_cost_cny"])),
            "payable_cny": "0.00",
            "fx_difference_cny": "0.00",
        })
    rows.sort(key=lambda row: (row["currency"], row["business_key"], row["reason_code"], row["doc_no"]))
    return rows


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def _ensure_acceptances(db, acceptances: list[dict]) -> None:
    for row in acceptances:
        existing = db.execute(
            "SELECT * FROM fx_acceptances WHERE acceptance_id = ?", (row["acceptance_id"],)
        ).fetchone()
        if existing is None:
            db.execute(
                "INSERT INTO fx_acceptances (acceptance_id, doc_no, line_no, sku, currency, "
                "original_amount, exchange_rate, received_on, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (row["acceptance_id"], row["doc_no"], row["line_no"], row["sku"], row["currency"],
                 fx.plain(row["original_amount"]), fx.plain(row["exchange_rate"]),
                 row["received_on"].isoformat()),
            )
            continue
        unchanged = (
            existing["doc_no"] == row["doc_no"]
            and existing["line_no"] == row["line_no"]
            and existing["sku"] == row["sku"]
            and existing["currency"] == row["currency"]
            and Decimal(existing["original_amount"]) == row["original_amount"]
            and Decimal(existing["exchange_rate"]) == row["exchange_rate"]
            and existing["received_on"] == row["received_on"].isoformat()
        )
        if not unchanged:
            raise DomainError(
                "VALUE_CONFLICT",
                f"验收 {row['acceptance_id']} 已存在且关键字段不一致，请使用新的验收标识",
            )


def _check_expected_balances(acceptances: list[dict], state: dict[str, dict]) -> None:
    """Reject write-offs planned against an out-of-date balance.

    ``expected_balance`` / ``version`` are optimistic-concurrency tokens supplied
    by the caller after reading the ledger; they are not part of the run digest.
    """
    for row in acceptances:
        expected_balance = row.get("expected_balance")
        expected_version = row.get("expected_version")
        if expected_balance is None and expected_version is None:
            continue
        current = state[row["acceptance_id"]]
        if expected_balance is not None:
            expected = fx.decimal_balance(expected_balance, "expected_balance")
            if expected != current["open_amount"]:
                raise DomainError(
                    "STALE_BALANCE",
                    f"验收 {row['acceptance_id']} 余额已变化：请求 {fx.plain(expected)}，"
                    f"当前 {fx.plain(current['open_amount'])}",
                    status=409,
                )
        if expected_version is not None:
            try:
                version = int(expected_version)
            except (TypeError, ValueError) as exc:
                raise DomainError("BAD_FORMAT", f"验收 {row['acceptance_id']} version 不是整数") from exc
            if version != current["version"]:
                raise DomainError(
                    "STALE_BALANCE",
                    f"验收 {row['acceptance_id']} 版本已变化：请求 {version}，当前 {current['version']}",
                    status=409,
                )


def _existing_invoice_conflicts(db, invoice: dict) -> bool:
    existing = db.execute(
        "SELECT * FROM fx_invoices WHERE invoice_id = ?", (invoice["invoice_id"],)
    ).fetchone()
    if existing is None:
        return False
    unchanged = (
        existing["doc_no"] == invoice["doc_no"]
        and existing["line_no"] == invoice["line_no"]
        and existing["sku"] == invoice["sku"]
        and existing["currency"] == invoice["currency"]
        and Decimal(existing["original_amount"]) == invoice["original_amount"]
        and Decimal(existing["exchange_rate"]) == invoice["exchange_rate"]
        and existing["invoiced_on"] == invoice["invoiced_on"].isoformat()
    )
    if not unchanged:
        raise DomainError(
            "VALUE_CONFLICT", f"发票 {invoice['invoice_id']} 已核销且关键字段不一致"
        )
    return True


def _post(db, acceptances: list[dict], invoices: list[dict], canon: bytes, window_days) -> dict:
    manifest = build_manifest(canon, len(acceptances), 0)
    created = store.record_run(manifest, db=db)
    _ensure_acceptances(db, acceptances)

    state = {row["acceptance_id"]: row for row in load_acceptances(db)}
    pending = [row for row in invoices if not _existing_invoice_conflicts(db, row)]
    pending.sort(key=lambda row: (row["invoiced_on"], row["invoice_id"]))
    if pending:
        # Concurrency tokens only matter when this call actually consumes balance;
        # a pure replay of an already posted batch must stay idempotent.
        _check_expected_balances(acceptances, state)
    for invoice in pending:
        allocations = fx.plan_invoice(invoice, list(state.values()), window_days)
        db.execute(
            "INSERT INTO fx_invoices (invoice_id, doc_no, line_no, sku, currency, original_amount, "
            "exchange_rate, invoiced_on, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice["invoice_id"], invoice["doc_no"], invoice["line_no"], invoice["sku"],
             invoice["currency"], fx.plain(invoice["original_amount"]),
             fx.plain(invoice["exchange_rate"]), invoice["invoiced_on"].isoformat(),
             manifest["run_id"]),
        )
        for allocation in allocations:
            db.execute(
                "INSERT INTO fx_writeoffs (writeoff_id, run_id, invoice_id, invoice_doc_no, "
                "acceptance_id, acceptance_doc_no, sku, currency, amount, cost_cny, payable_cny, "
                "fx_difference_cny, invoice_rate, receipt_rate, seq, tolerance_residual) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{manifest['run_id']}:{invoice['invoice_id']}:{allocation['acceptance_id']}",
                 manifest["run_id"], invoice["invoice_id"], invoice["doc_no"],
                 allocation["acceptance_id"], allocation["acceptance_doc_no"],
                 invoice["sku"], invoice["currency"],
                 fx.plain(allocation["amount"]), fx.plain(allocation["cost_cny"]),
                 fx.plain(allocation["payable_cny"]), fx.plain(allocation["fx_difference_cny"]),
                 fx.plain(invoice["exchange_rate"]), fx.plain(allocation["receipt_rate"]),
                 allocation["sequence"], fx.plain(allocation["rounding_residual_cny"])),
            )
            state[allocation["acceptance_id"]]["version"] += 1
            state[allocation["acceptance_id"]]["open_amount"] -= allocation["amount"]
            state[allocation["acceptance_id"]]["open_cost"] -= allocation["cost_cny"]
            db.execute(
                "UPDATE fx_acceptances SET version = version + 1 WHERE acceptance_id = ?",
                (allocation["acceptance_id"],),
            )
        payable_total = fx.invoice_payable(invoice["original_amount"], invoice["exchange_rate"])
        cost_total = fx.money(sum((item["cost_cny"] for item in allocations), Decimal("0")))
        fx_amount = payable_total - cost_total
        if fx_amount != 0:
            db.execute(
                "INSERT INTO fx_differences (difference_id, run_id, invoice_id, invoice_doc_no, "
                "line_no, sku, currency, invoiced, cost_cny, payable_cny, fx_difference_cny, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "strftime('%Y-%m-%dT%H:%M:%S+08:00','now','localtime'))",
                (f"{manifest['run_id']}:{invoice['invoice_id']}", manifest["run_id"],
                 invoice["invoice_id"], invoice["doc_no"], invoice["line_no"],
                 invoice["sku"], invoice["currency"],
                 fx.plain(invoice["original_amount"]), fx.plain(cost_total),
                 fx.plain(payable_total), fx.plain(fx_amount)),
            )
    return result_for(db, manifest["run_id"], created)


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------

def result_for(db, run_id_value: str, created: bool | None = None) -> dict:
    """Assemble the full ledger view for the current database state."""
    groups = load_groups(db)
    invoices = load_invoices(db)
    acceptances = load_acceptances(db)
    counts = {
        "acceptances": len(acceptances),
        "invoices": len(invoices),
        "writeoffs": db.execute("SELECT COUNT(*) FROM fx_writeoffs").fetchone()[0],
        "differences": db.execute("SELECT COUNT(*) FROM fx_differences").fetchone()[0],
    }
    per_currency = fx.totals_by_currency(groups)
    result = {
        "run_id": run_id_value,
        "created": created,
        "acceptances": [{
            "acceptance_id": row["acceptance_id"],
            "doc_no": row["doc_no"],
            "line_no": row["line_no"],
            "sku": row["sku"],
            "currency": row["currency"],
            "original_amount": fx.amount_text(row["original_amount"]),
            "exchange_rate": fx.plain(row["exchange_rate"]),
            "received_on": row["received_on"].isoformat(),
            "cost_cny": fx.plain(row["cost_cny"]),
            "written_off": fx.amount_text(row["written_off"]),
            "open_amount": fx.amount_text(row["open_amount"]),
            "open_cost_cny": fx.plain(fx.money(row["open_cost"])),
            "version": row["version"],
        } for row in acceptances],
        "invoices": [{
            "invoice_id": row["invoice_id"],
            "doc_no": row["doc_no"],
            "line_no": row["line_no"],
            "sku": row["sku"],
            "currency": row["currency"],
            "original_amount": fx.amount_text(row["original_amount"]),
            "exchange_rate": fx.plain(row["exchange_rate"]),
            "invoiced_on": row["invoiced_on"].isoformat(),
            "payable_cny": fx.plain(row["payable_cny"]),
            "cost_cny": fx.plain(row["cost_cny"]),
            "fx_difference_cny": fx.plain(row["fx_difference_cny"]),
            "status": row["status"],
            "allocations": [{
                "acceptance_id": item["acceptance_id"],
                "acceptance_doc_no": item["acceptance_doc_no"],
                "sequence": item["sequence"],
                "amount": fx.amount_text(item["amount"]),
                "receipt_rate": fx.plain(item["receipt_rate"]),
                "invoice_rate": fx.plain(item["invoice_rate"]),
                "cost_cny": fx.plain(item["cost_cny"]),
                "payable_cny": fx.plain(item["payable_cny"]),
                "fx_difference_cny": fx.plain(item["fx_difference_cny"]),
            } for item in row["allocations"]],
        } for row in invoices],
        "groups": [{
            "sku": row["sku"],
            "currency": row["currency"],
            "received": fx.amount_text(row["received"]),
            "written_off": fx.amount_text(row["written_off"]),
            "uninvoiced": fx.amount_text(row["uninvoiced"]),
            "cost_cny": fx.plain(fx.money(row["cost_cny"])),
            "payable_cny": fx.plain(fx.money(row["payable_cny"])),
            "fx_difference_cny": fx.plain(fx.money(row["fx_difference_cny"])),
            "uninvoiced_cost_cny": fx.plain(fx.money(row["uninvoiced_cost_cny"])),
        } for row in groups],
        "currency_totals": per_currency,
        "differences": _difference_rows(db),
        "counts": counts,
        "reconcile": [{
            "sku": row["sku"],
            "currency": row["currency"],
            "received": fx.amount_text(row["received"]),
            "written_off": fx.amount_text(row["written_off"]),
            "uninvoiced": fx.amount_text(row["uninvoiced"]),
            "difference": fx.amount_text(row["received"] - row["written_off"] - row["uninvoiced"]),
            "balanced": row["received"] == row["written_off"] + row["uninvoiced"],
        } for row in groups],
    }
    result["balanced"] = all(item["balanced"] for item in result["reconcile"])
    return result


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run(payload: dict | None = None) -> dict:
    payload = payload or {}
    # Sample data only for a truly empty request (no body / `{}`); an explicit
    # form submission with empty lists must not silently post the sample batch.
    use_sample = payload == {}
    raw_acceptances = payload.get("acceptances") if not use_sample else SAMPLE_ACCEPTANCES
    raw_invoices = payload.get("invoices") if not use_sample else SAMPLE_INVOICES
    window_days = payload.get("window_days")
    acceptances = fx.normalize_acceptances(raw_acceptances or [])
    invoices = fx.normalize_invoices(raw_invoices or [])
    canon = fx.canonical_input(acceptances, invoices, window_days)
    digest = build_manifest(canon, len(acceptances), 0)["run_id"]
    with store.transaction() as db:
        pending = [row for row in invoices
                   if db.execute("SELECT 1 FROM fx_invoices WHERE invoice_id = ?",
                                 (row["invoice_id"],)).fetchone() is None]
        posted = db.execute("SELECT 1 FROM runs WHERE run_id = ?", (digest,)).fetchone()
        if posted and not pending:
            return result_for(db, digest, False)
        return _post(db, acceptances, invoices, canon, window_days)


def demo():
    return run()


def differences_for_export() -> list[dict]:
    with store.snapshot() as db:
        data = result_for(db, "", None)
    if not data["acceptances"]:
        return _sample_export_rows()
    return [{
        "business_key": row["business_key"],
        "doc_no": row["doc_no"],
        "line_no": row["line_no"],
        "reason_code": row["reason_code"],
        "currency": row["currency"],
        "original_amount": row["original_amount"],
        "amount_cny": row["amount_cny"],
        "cost_cny": row["cost_cny"],
        "payable_cny": row["payable_cny"],
        "fx_difference_cny": row["fx_difference_cny"],
    } for row in data["differences"]]


def _sample_export_rows() -> list[dict]:
    """Export preview before any batch is posted (the sample batch, not persisted)."""
    acceptances = [
        {**fx.normalize_acceptances([row])[0],
         "cost_cny": fx.acceptance_cost(fx.decimal_amount(row["original_amount"]),
                                        fx.decimal_rate(row["exchange_rate"])),
         }
        for row in SAMPLE_ACCEPTANCES
    ]
    for row in acceptances:
        row["open_amount"] = row["original_amount"]
        row["open_cost"] = row["cost_cny"]
    rows = []
    for raw in SAMPLE_INVOICES:
        invoice = fx.normalize_invoices([raw])[0]
        allocations = fx.plan_invoice(invoice, acceptances)
        for allocation in allocations:
            for row in acceptances:
                if row["acceptance_id"] == allocation["acceptance_id"]:
                    row["open_amount"] -= allocation["amount"]
                    row["open_cost"] -= allocation["cost_cny"]
        payable = fx.invoice_payable(invoice["original_amount"], invoice["exchange_rate"])
        cost = fx.money(sum((item["cost_cny"] for item in allocations), Decimal("0")))
        if payable != cost:
            rows.append({
                "business_key": f"{invoice['sku']}|{invoice['currency']}",
                "doc_no": invoice["doc_no"], "line_no": 0, "reason_code": "FX_DIFFERENCE",
                "currency": invoice["currency"],
                "original_amount": fx.amount_text(invoice["original_amount"]),
                "amount_cny": fx.plain(payable - cost),
                "cost_cny": fx.plain(cost), "payable_cny": fx.plain(payable),
                "fx_difference_cny": fx.plain(payable - cost),
            })
    for row in acceptances:
        if row["open_amount"] == 0:
            continue
        rows.append({
            "business_key": f"{row['sku']}|{row['currency']}",
            "doc_no": "", "line_no": 0, "reason_code": "UNINVOICED",
            "currency": row["currency"],
            "original_amount": fx.amount_text(row["open_amount"]),
            "amount_cny": fx.plain(fx.money(row["open_cost"])),
            "cost_cny": fx.plain(fx.money(row["open_cost"])),
            "payable_cny": "0.00", "fx_difference_cny": "0.00",
        })
    return rows
