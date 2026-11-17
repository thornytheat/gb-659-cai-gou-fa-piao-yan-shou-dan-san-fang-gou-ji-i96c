"""Foreign-currency acceptance / invoice write-off rules.

Accounting model
----------------
* Acceptance (GR) cost stays at the *receipt-day* rate: cost_cny = round(original * receipt_rate).
  Inventory cost is never re-priced when an invoice arrives at another rate.
* Invoice payable is recognised at the *invoice-day* rate:
  payable_cny = round(original * invoice_rate).
* Write-off happens in the **original currency**.  One invoice line is allocated
  against acceptance lines of the same SKU and the same currency, FIFO by
  receipt date.  Different currencies are never netted.
* Each allocation records three amounts side by side: cost_cny (receipt rate),
  payable_cny (invoice rate) and fx_difference_cny = payable - cost.  A CNY
  amount difference caused by rates therefore never looks like an under-invoice.
* Partially invoiced receipts consume only the invoiced original amount; the
  untouched original balance stays open.
* Cent rounding residuals are absorbed by the *last* allocation: the last
  allocation of an invoice absorbs the payable residual so the invoice's
  payable equals round(original * invoice_rate); the allocation that closes an
  acceptance absorbs the cost residual so the acceptance's booked cost is
  fully released.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.errors import DomainError
from app.reasons import REASONS

CENT = Decimal("0.01")
NULLS = {"", "-", "N/A", "NULL", "NONE", "NIL"}
CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def plain(value: Decimal) -> str:
    return format(Decimal(value), "f")


def amount_text(value: Decimal) -> str:
    """Original-currency amounts are always rendered to cents for the ledger."""
    return format(money(value), "f")


def decimal_amount(value, field: str = "amount") -> Decimal:
    token = str(value).strip().replace(",", "").replace("，", "").replace("￥", "").replace("$", "")
    if token.upper() in NULLS:
        raise DomainError("EMPTY_VALUE", f"{field} 不能为空")
    try:
        number = Decimal(token)
    except InvalidOperation as exc:
        raise DomainError("BAD_FORMAT", f"{field} 不是合法数字：{value}") from exc
    if number <= 0:
        raise DomainError("NON_POSITIVE_AMOUNT", f"{field} 必须大于零：{value}")
    return number


def decimal_balance(value, field: str = "balance") -> Decimal:
    token = str(value).strip().replace(",", "").replace("，", "")
    if token.upper() in NULLS:
        raise DomainError("EMPTY_VALUE", f"{field} 不能为空")
    try:
        number = Decimal(token)
    except InvalidOperation as exc:
        raise DomainError("BAD_FORMAT", f"{field} 不是合法数字：{value}") from exc
    if number < 0:
        raise DomainError("BAD_FORMAT", f"{field} 不能为负：{value}")
    return number


def decimal_rate(value, field: str = "exchange_rate") -> Decimal:
    token = str(value).strip().replace("%", "")
    if token.upper() in NULLS:
        raise DomainError("EMPTY_VALUE", f"{field} 不能为空")
    try:
        rate = Decimal(token)
    except InvalidOperation as exc:
        raise DomainError("BAD_FORMAT", f"{field} 不是合法汇率：{value}") from exc
    if rate <= 0:
        raise DomainError("BAD_RATE", f"{field} 必须大于零：{value}")
    return rate


def currency_code(value) -> str:
    token = str(value or "").strip().upper()
    if not CURRENCY_CODE.match(token):
        raise DomainError("BAD_CURRENCY", f"币种必须为三位字母代码：{value}")
    return token


def business_date(value, field: str = "date") -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise DomainError("BAD_FORMAT", f"{field} 必须是 YYYY-MM-DD：{value}") from exc


def reason(code: str) -> str:
    return REASONS.get(code, code)


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------

def _first(row: dict, *names, default=None):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def normalize_acceptances(rows: list[dict]) -> list[dict]:
    result, seen = [], {}
    for index, row in enumerate(rows or [], start=2):
        if not isinstance(row, dict):
            raise DomainError("BAD_FORMAT", f"验收第 {index} 行不是对象")
        doc_no = str(_first(row, "doc_no", "acceptance_doc_no", "gr_no", "document_no", default="")).strip()
        if not doc_no:
            raise DomainError("EMPTY_VALUE", f"验收第 {index} 行缺少 doc_no")
        line_no = int(_first(row, "line_no", default=index) or index)
        acceptance_id = str(_first(row, "acceptance_id", "gr_id", default=f"{doc_no}#{line_no}"))
        normalized = {
            "acceptance_id": acceptance_id,
            "doc_no": doc_no,
            "line_no": line_no,
            "sku": str(_first(row, "sku", "item_code", "product_code", default="")).strip(),
            "currency": currency_code(_first(row, "currency", "currency_code", default="CNY")),
            "original_amount": decimal_amount(
                _first(row, "original_amount", "amount", "foreign_amount", "currency_amount"),
                "original_amount",
            ),
            "exchange_rate": decimal_rate(_first(row, "exchange_rate", "rate", "fx_rate")),
            "received_on": business_date(
                _first(row, "received_on", "receipt_date", "gr_date", "date"), "received_on"
            ),
            "expected_balance": _first(row, "expected_balance", "expected_open_amount"),
            "expected_version": _first(row, "version", "expected_version"),
            "row_no": index,
        }
        if not normalized["sku"]:
            raise DomainError("EMPTY_VALUE", f"验收第 {index} 行缺少 sku")
        if acceptance_id in seen:
            existing = seen[acceptance_id]
            same = all(existing[key] == normalized[key] for key in
                       ("doc_no", "line_no", "sku", "currency", "original_amount", "exchange_rate", "received_on"))
            raise DomainError("VALUE_CONFLICT" if not same else "DUPLICATE_KEY",
                              f"验收标识重复：{acceptance_id}")
        seen[acceptance_id] = normalized
        result.append(normalized)
    return result


def normalize_invoices(rows: list[dict]) -> list[dict]:
    result, seen = [], {}
    for index, row in enumerate(rows or [], start=2):
        if not isinstance(row, dict):
            raise DomainError("BAD_FORMAT", f"发票第 {index} 行不是对象")
        doc_no = str(_first(row, "doc_no", "invoice_doc_no", "iv_no", "document_no", default="")).strip()
        if not doc_no:
            raise DomainError("EMPTY_VALUE", f"发票第 {index} 行缺少 doc_no")
        line_no = int(_first(row, "line_no", default=index) or index)
        invoice_id = str(_first(row, "invoice_id", "iv_id", default=f"{doc_no}#{line_no}"))
        normalized = {
            "invoice_id": invoice_id,
            "doc_no": doc_no,
            "line_no": line_no,
            "sku": str(_first(row, "sku", "item_code", "product_code", default="")).strip(),
            "currency": currency_code(_first(row, "currency", "currency_code", default="CNY")),
            "original_amount": decimal_amount(
                _first(row, "original_amount", "amount", "foreign_amount", "currency_amount"),
                "original_amount",
            ),
            "exchange_rate": decimal_rate(_first(row, "exchange_rate", "rate", "fx_rate")),
            "invoiced_on": business_date(
                _first(row, "invoiced_on", "invoice_date", "iv_date", "date"), "invoiced_on"
            ),
            "row_no": index,
        }
        if not normalized["sku"]:
            raise DomainError("EMPTY_VALUE", f"发票第 {index} 行缺少 sku")
        if invoice_id in seen:
            raise DomainError("DUPLICATE_KEY", f"发票标识重复：{invoice_id}")
        seen[invoice_id] = normalized
        result.append(normalized)
    return result


def canonical_input(acceptances: list[dict], invoices: list[dict], window_days=None) -> bytes:
    """Deterministic business payload used for the idempotent run id.

    Concurrency tokens (expected_balance / version) are deliberately excluded:
    re-submitting the same business facts must reuse the same run.
    """
    import json

    def acceptance(row):
        return {key: plain(row[key]) if isinstance(row[key], Decimal) else
                row[key].isoformat() if isinstance(row[key], date) else row[key]
                for key in ("acceptance_id", "doc_no", "line_no", "sku", "currency",
                            "original_amount", "exchange_rate", "received_on")}

    def invoice(row):
        return {key: plain(row[key]) if isinstance(row[key], Decimal) else
                row[key].isoformat() if isinstance(row[key], date) else row[key]
                for key in ("invoice_id", "doc_no", "line_no", "sku", "currency",
                            "original_amount", "exchange_rate", "invoiced_on")}

    payload = {
        "acceptances": [acceptance(row) for row in acceptances],
        "invoices": [invoice(row) for row in invoices],
        "window_days": window_days,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def acceptance_cost(original_amount: Decimal, receipt_rate: Decimal) -> Decimal:
    return money(original_amount * receipt_rate)


def invoice_payable(original_amount: Decimal, invoice_rate: Decimal) -> Decimal:
    return money(original_amount * invoice_rate)


def eligible_candidates(invoice: dict, acceptances: list[dict], window_days=None) -> list[dict]:
    candidates = []
    for row in acceptances:
        if row["currency"] != invoice["currency"]:
            continue  # 币种不互抵
        if row["sku"] != invoice["sku"]:
            continue
        if row["open_amount"] <= 0:
            continue
        if window_days is not None:
            if abs((invoice["invoiced_on"] - row["received_on"]).days) > int(window_days):
                continue
        candidates.append(row)
    return sorted(candidates, key=lambda row: (row["received_on"], row["acceptance_id"]))


def plan_invoice(invoice: dict, acceptances: list[dict], window_days=None) -> list[dict]:
    """Allocate an invoice in original currency against acceptance balances.

    Allocation is FIFO by receipt date within the same SKU + currency.  Money is
    recognised with *cumulative* rounding: each slice books the difference
    between the rounded cumulative amount and what was already booked, so the
    final slice always carries the cent residual ("尾差留在最后一笔") while no
    slice can go negative.
    """
    candidates = eligible_candidates(invoice, acceptances, window_days)
    remaining = invoice["original_amount"]
    chosen = []
    for candidate in candidates:
        if remaining <= 0:
            break
        take = min(candidate["open_amount"], remaining)
        chosen.append((candidate, take))
        remaining -= take
    if remaining > 0:
        available = invoice["original_amount"] - remaining
        raise DomainError(
            "INSUFFICIENT_ACCEPTANCE",
            f"发票 {invoice['doc_no']} 在 {invoice['currency']} 下仅有 {plain(available)} 可核销，"
            f"缺少 {plain(remaining)}（币种不互抵）",
        )

    allocations = []
    working = {row["acceptance_id"]: dict(row) for row, _ in chosen}
    released = {row["acceptance_id"]: row["cost_cny"] - row["open_cost"] for row, _ in chosen}
    payable_booked = Decimal("0")
    payable_total = invoice_payable(invoice["original_amount"], invoice["exchange_rate"])
    for position, (candidate, take) in enumerate(chosen):
        current = working[candidate["acceptance_id"]]
        # Cost follows cumulative recognition within an acceptance, so the slice
        # closing it releases exactly the remaining inventory cost (never negative).
        written_before = current["original_amount"] - current["open_amount"]
        cost_cumulative = acceptance_cost(written_before + take, current["exchange_rate"])
        cost = cost_cumulative - released[candidate["acceptance_id"]]
        released[candidate["acceptance_id"]] = cost_cumulative

        if position == len(chosen) - 1:
            # 尾差留在最后一笔：the last slice carries the invoice's cent residual,
            # so the sum equals round(original × invoice rate) exactly.
            payable = payable_total - payable_booked
        else:
            payable = invoice_payable(take, invoice["exchange_rate"])
        payable_booked += payable

        naive_cost = acceptance_cost(take, candidate["exchange_rate"])
        naive_payable = invoice_payable(take, invoice["exchange_rate"])
        allocations.append({
            "acceptance_id": candidate["acceptance_id"],
            "acceptance_doc_no": candidate["doc_no"],
            "line_no": candidate["line_no"],
            "sku": invoice["sku"],
            "currency": invoice["currency"],
            "amount": take,
            "receipt_rate": candidate["exchange_rate"],
            "invoice_rate": invoice["exchange_rate"],
            "cost_cny": cost,
            "payable_cny": payable,
            "fx_difference_cny": payable - cost,
            "rounding_residual_cny": (cost - naive_cost) + (payable - naive_payable),
            "sequence": position + 1,
            "closes_acceptance": take == current["open_amount"],
        })
        current["open_amount"] -= take
        current["open_cost"] -= cost
    return allocations


def totals_by_currency(groups: list[dict]) -> list[dict]:
    buckets = {}
    for group in groups:
        bucket = buckets.setdefault(group["currency"], {
            "currency": group["currency"],
            "received": Decimal("0"),
            "written_off": Decimal("0"),
            "uninvoiced": Decimal("0"),
            "cost_cny": Decimal("0"),
            "payable_cny": Decimal("0"),
            "fx_difference_cny": Decimal("0"),
        })
        for key in ("received", "written_off", "uninvoiced", "cost_cny", "payable_cny", "fx_difference_cny"):
            bucket[key] += group[key]
    result = []
    for code in sorted(buckets):
        bucket = buckets[code]
        row = {"currency": code}
        for key in ("received", "written_off", "uninvoiced"):
            row[key] = plain(bucket[key])
        for key in ("cost_cny", "payable_cny", "fx_difference_cny"):
            row[key] = plain(money(bucket[key]))
        result.append(row)
    return result
