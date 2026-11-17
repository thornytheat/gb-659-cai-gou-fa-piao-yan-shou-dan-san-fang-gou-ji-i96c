from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.store as store
from app.main import app
from app.modules.m6_batches import fx, service

ACCEPTANCES = [
    {"acceptance_id": "GR-1#1", "doc_no": "GR-1", "line_no": 1, "sku": "SKU-A", "currency": "USD",
     "original_amount": "1000.00", "exchange_rate": "7.10", "received_on": "2026-01-05"},
    {"acceptance_id": "GR-2#1", "doc_no": "GR-2", "line_no": 1, "sku": "SKU-A", "currency": "USD",
     "original_amount": "500.00", "exchange_rate": "7.20", "received_on": "2026-01-20"},
]
INVOICE = {"invoice_id": "IV-1#1", "doc_no": "IV-1", "line_no": 1, "sku": "SKU-A", "currency": "USD",
           "original_amount": "1200.00", "exchange_rate": "7.30", "invoiced_on": "2026-02-10"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "db.sqlite")


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Cost / payable / FX separation
# ---------------------------------------------------------------------------

def test_cost_keeps_receipt_rate_and_payable_uses_invoice_rate(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    data = client.post("/api/reconcile", json=body).json()

    assert data["acceptances"][0]["cost_cny"] == "7100.00"      # 1000 × 7.10, receipt day
    assert data["acceptances"][1]["cost_cny"] == "3600.00"      # 500 × 7.20
    invoice = data["invoices"][0]
    assert invoice["payable_cny"] == "8760.00"                  # 1200 × 7.30, invoice day
    assert invoice["cost_cny"] == "8540.00"                     # 7100 + 200 × 7.20
    assert invoice["fx_difference_cny"] == "220.00"             # payable − cost


def test_fx_difference_is_not_under_invoicing(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    data = client.post("/api/reconcile", json=body).json()

    invoice = data["invoices"][0]
    assert Decimal(invoice["original_amount"]) == (
        sum(Decimal(item["amount"]) for item in invoice["allocations"])
    )
    codes = {row["reason_code"] for row in data["differences"]}
    assert "FX_DIFFERENCE" in codes
    fx_rows = [row for row in data["differences"] if row["reason_code"] == "FX_DIFFERENCE"]
    assert fx_rows[0]["amount_cny"] == "220.00"
    # The missing 300 USD is the only UNINVOICED row; FX never moves that amount.
    uninvoiced = [row for row in data["differences"] if row["reason_code"] == "UNINVOICED"]
    assert [row["original_amount"] for row in uninvoiced] == ["300.00"]


def test_cost_is_never_repriced_by_invoice_rate(client):
    before = client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": []}).json()
    after = client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [INVOICE]}).json()
    costs = {row["acceptance_id"]: row["cost_cny"] for row in before["acceptances"]}
    for row in after["acceptances"]:
        assert row["cost_cny"] == costs[row["acceptance_id"]]


# ---------------------------------------------------------------------------
# Partial invoicing, residual, currency isolation
# ---------------------------------------------------------------------------

def test_partial_invoice_only_consumes_matching_original_amount(client):
    invoice = {**INVOICE, "original_amount": "600.00"}
    data = client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [invoice]}).json()

    first, second = data["acceptances"]
    assert first["written_off"] == "600.00" and first["open_amount"] == "400.00"
    assert second["written_off"] == "0.00" and second["open_amount"] == "500.00"
    assert data["groups"][0]["uninvoiced"] == "900.00"


def test_fifo_allocates_oldest_acceptance_first(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    invoice = client.post("/api/reconcile", json=body).json()["invoices"][0]
    assert [item["acceptance_id"] for item in invoice["allocations"]] == ["GR-1#1", "GR-2#1"]
    assert [item["amount"] for item in invoice["allocations"]] == ["1000.00", "200.00"]


def test_rounding_residual_stays_on_last_allocation():
    # 3 × 1.005 keeps its cent residual in the final slice; cumulative amounts stay exact.
    acceptances = [
        {"acceptance_id": "G1", "doc_no": "G1", "line_no": 1, "sku": "SKU-R", "currency": "USD",
         "original_amount": "1.00", "exchange_rate": "1.00", "received_on": "2026-01-01"},
        {"acceptance_id": "G2", "doc_no": "G2", "line_no": 1, "sku": "SKU-R", "currency": "USD",
         "original_amount": "1.00", "exchange_rate": "1.00", "received_on": "2026-01-02"},
        {"acceptance_id": "G3", "doc_no": "G3", "line_no": 1, "sku": "SKU-R", "currency": "USD",
         "original_amount": "1.00", "exchange_rate": "1.00", "received_on": "2026-01-03"},
    ]
    state = []
    for row in acceptances:
        amount = Decimal(row["original_amount"])
        cost = fx.acceptance_cost(amount, Decimal(row["exchange_rate"]))
        state.append({**row, "original_amount": amount, "exchange_rate": Decimal(row["exchange_rate"]),
                      "received_on": fx.business_date(row["received_on"]),
                      "cost_cny": cost, "open_amount": amount, "open_cost": cost})
    invoice = {
        "invoice_id": "IV-R", "doc_no": "IV-R", "line_no": 1, "sku": "SKU-R", "currency": "USD",
        "original_amount": Decimal("3.00"), "exchange_rate": Decimal("1.005"),
        "invoiced_on": fx.business_date("2026-03-01"),
    }
    allocations = fx.plan_invoice(invoice, state)
    payable_total = fx.invoice_payable(Decimal("3.00"), Decimal("1.005"))
    assert payable_total == Decimal("3.02")  # 3.015 half-up to cent
    assert [item["payable_cny"] for item in allocations] == [
        Decimal("1.01"), Decimal("1.01"), Decimal("1.00")
    ]
    assert sum(item["payable_cny"] for item in allocations) == payable_total
    # Cumulative recognition: 1.01 + 1.01 + 1.00 = 3.02; the residual sits on the
    # final slice rather than being spread across the batch.
    assert allocations[-1]["rounding_residual_cny"] == Decimal("-0.01")
    assert all(item["payable_cny"] >= 0 for item in allocations)


def test_currencies_never_offset(client):
    acceptances = ACCEPTANCES + [{
        "acceptance_id": "GR-E#1", "doc_no": "GR-E", "sku": "SKU-A", "currency": "EUR",
        "original_amount": "900.00", "exchange_rate": "7.80", "received_on": "2026-01-06",
    }]
    invoice = {**INVOICE, "invoice_id": "IV-E#1", "doc_no": "IV-E", "original_amount": "900.00",
               "currency": "EUR", "exchange_rate": "8.00"}
    data = client.post("/api/reconcile", json={"acceptances": acceptances,
                                               "invoices": [INVOICE, invoice]}).json()
    usd = next(row for row in data["groups"] if row["currency"] == "USD")
    eur = next(row for row in data["groups"] if row["currency"] == "EUR")
    assert usd["received"] == "1500.00" and usd["written_off"] == "1200.00"
    assert eur["received"] == "900.00" and eur["written_off"] == "900.00"
    assert {row["currency"] for row in data["currency_totals"]} == {"USD", "EUR"}


def test_insufficient_same_currency_balance_rejected(client):
    invoice = {**INVOICE, "original_amount": "1600.00"}
    response = client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [invoice]})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INSUFFICIENT_ACCEPTANCE"


def test_acceptance_cannot_be_over_written_off(client):
    first = {**INVOICE, "invoice_id": "IV-A", "doc_no": "IV-A", "original_amount": "1200.00"}
    second = {**INVOICE, "invoice_id": "IV-B", "doc_no": "IV-B", "original_amount": "400.00"}
    client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [first]})
    response = client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [second]})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INSUFFICIENT_ACCEPTANCE"


# ---------------------------------------------------------------------------
# Concurrency and atomicity
# ---------------------------------------------------------------------------

def test_stale_balance_is_rejected(client):
    client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [INVOICE]})
    stale = [{**row, "expected_balance": "1000.00" if row["acceptance_id"] == "GR-1#1" else "500.00"}
             for row in ACCEPTANCES]
    invoice = {**INVOICE, "invoice_id": "IV-2#1", "doc_no": "IV-2", "original_amount": "100.00"}
    response = client.post("/api/reconcile", json={"acceptances": stale, "invoices": [invoice]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_BALANCE"


def test_stale_version_is_rejected(client):
    client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [INVOICE]})
    stale = [{**row, "version": 0} for row in ACCEPTANCES]
    invoice = {**INVOICE, "invoice_id": "IV-2#1", "doc_no": "IV-2", "original_amount": "100.00"}
    response = client.post("/api/reconcile", json={"acceptances": stale, "invoices": [invoice]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_BALANCE"


def test_stale_version_rejected_even_when_balance_still_fits(client):
    # A used acceptance may still have balance; only its version proves the page
    # state is current. A partial invoice leaves a non-zero balance behind.
    partial = {**INVOICE, "original_amount": "100.00"}
    client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [partial]})
    stale = [{**row, "version": 0} for row in ACCEPTANCES]
    next_invoice = {**INVOICE, "invoice_id": "IV-2#1", "doc_no": "IV-2", "original_amount": "100.00"}
    response = client.post("/api/reconcile", json={"acceptances": stale, "invoices": [next_invoice]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_BALANCE"
    # Same request with the current version posts.
    with store.snapshot() as db:
        current = {row["acceptance_id"]: row for row in service.load_acceptances(db)}
    fresh = [{**row, "version": current[row["acceptance_id"]]["version"],
              "expected_balance": fx.plain(current[row["acceptance_id"]]["open_amount"])}
             for row in ACCEPTANCES]
    ok = client.post("/api/reconcile", json={"acceptances": fresh, "invoices": [next_invoice]})
    assert ok.status_code == 200


def test_replay_with_old_versions_still_idempotent(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    first = client.post("/api/reconcile", json=body).json()
    # Concurrency tokens are not part of the digest: replay with them must not
    # create a new run, and there is nothing pending so they cannot trip 409.
    replay = [{**row, "version": 0, "expected_balance": "1000.00"} for row in ACCEPTANCES]
    second = client.post("/api/reconcile", json={"acceptances": replay, "invoices": [INVOICE]}).json()
    assert second["created"] is False and second["run_id"] == first["run_id"]


def test_failed_posting_restores_previous_writeoffs(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    before = client.post("/api/reconcile", json=body).json()
    writeoffs_before = store.fx_writeoff_count()
    differences_before = store.fx_difference_count()

    # Second invoice exceeds the remaining USD balance: the whole batch must roll back.
    too_big = {**INVOICE, "invoice_id": "IV-9#1", "doc_no": "IV-9", "original_amount": "999.00"}
    response = client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [too_big]})
    assert response.status_code == 422
    assert store.fx_writeoff_count() == writeoffs_before
    assert store.fx_difference_count() == differences_before

    after = client.get("/api/export/differences.csv")
    assert after.status_code == 200
    assert client.post("/api/reconcile", json=body).json()["created"] is False
    assert before["run_id"] == client.post("/api/reconcile", json=body).json()["run_id"]


def test_failure_after_partial_insert_rolls_back_everything(client):
    # First invoice fits; second does not. The first insert must be undone too.
    first = {**INVOICE, "invoice_id": "IV-OK#1", "doc_no": "IV-OK", "original_amount": "300.00"}
    second = {**INVOICE, "invoice_id": "IV-BAD#1", "doc_no": "IV-BAD", "original_amount": "1400.00"}
    response = client.post("/api/reconcile", json={"acceptances": ACCEPTANCES,
                                                   "invoices": [first, second]})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INSUFFICIENT_ACCEPTANCE"
    assert store.fx_writeoff_count() == 0
    assert store.fx_difference_count() == 0
    # Retrying without the bad invoice posts normally from a clean slate.
    ok = client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [first]}).json()
    assert ok["created"] is True
    assert store.fx_writeoff_count() == 1


def test_changing_acceptance_rate_is_rejected(client):
    client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": []})
    repriced = [{**row, "exchange_rate": "6.50"} if row["acceptance_id"] == "GR-1#1" else row
                for row in ACCEPTANCES]
    response = client.post("/api/reconcile", json={"acceptances": repriced, "invoices": []})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALUE_CONFLICT"


def test_changing_original_amount_is_rejected(client):
    client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": []})
    changed = [{**row, "original_amount": "999.00"} if row["acceptance_id"] == "GR-1#1" else row
               for row in ACCEPTANCES]
    response = client.post("/api/reconcile", json={"acceptances": changed, "invoices": []})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALUE_CONFLICT"


def test_no_fx_row_when_rates_match(client):
    same_rate = [{"acceptance_id": "GR-S#1", "doc_no": "GR-S", "line_no": 1, "sku": "SKU-S",
                  "currency": "USD", "original_amount": "1000.00", "exchange_rate": "7.10",
                  "received_on": "2026-01-05"}]
    invoice = {**INVOICE, "invoice_id": "IV-S#1", "doc_no": "IV-S", "sku": "SKU-S",
               "original_amount": "1000.00", "exchange_rate": "7.10"}
    data = client.post("/api/reconcile", json={"acceptances": same_rate,
                                               "invoices": [invoice]}).json()
    assert data["differences"] == []
    assert data["invoices"][0]["fx_difference_cny"] == "0.00"
    assert data["invoices"][0]["payable_cny"] == data["invoices"][0]["cost_cny"] == "7100.00"



def test_rerun_does_not_duplicate_writeoffs_or_fx_rows(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    first = client.post("/api/reconcile", json=body).json()
    counts = (store.fx_writeoff_count(), store.fx_difference_count())
    second = client.post("/api/reconcile", json=body).json()
    third = client.post("/api/reconcile", json=body).json()

    assert first["created"] is True
    assert second["created"] is False and third["created"] is False
    assert second["run_id"] == first["run_id"]
    assert (store.fx_writeoff_count(), store.fx_difference_count()) == counts
    assert second["counts"] == first["counts"]


def test_reconcile_is_idempotent_for_sample(client):
    first = client.post("/api/reconcile").json()
    second = client.post("/api/reconcile").json()
    assert first["run_id"] == second["run_id"]
    assert second["created"] is False


# ---------------------------------------------------------------------------
# Ledger / export / page
# ---------------------------------------------------------------------------

def test_ledger_reconciles_within_currency(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    data = client.post("/api/reconcile", json=body).json()
    assert data["balanced"] is True
    for row in data["reconcile"]:
        assert Decimal(row["received"]) == Decimal(row["written_off"]) + Decimal(row["uninvoiced"])


def test_export_lists_cost_payable_and_fx_separately(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    client.post("/api/reconcile", json=body)
    text = client.get("/api/export/differences.csv").text
    assert "cost_cny" in text and "payable_cny" in text and "fx_difference_cny" in text
    assert "FX_DIFFERENCE" in text and "220.00" in text


def test_batch_page_shows_costs_and_fx(client):
    body = {"acceptances": ACCEPTANCES, "invoices": [INVOICE]}
    client.post("/api/reconcile", json=body)
    page = client.get("/batches").text
    assert "收货日汇率" in page and "开票日汇率" in page
    assert "7100.00" in page and "8760.00" in page and "220.00" in page


def test_sample_batch_page_runs(client):
    assert client.get("/batches").status_code == 200
    response = client.post("/api/reconcile")
    assert response.status_code == 200
    assert response.json()["currency_totals"]


# ---------------------------------------------------------------------------
# Page input round-trip
# ---------------------------------------------------------------------------

def test_page_exposes_versions_for_concurrency(client):
    client.post("/api/reconcile", json={"acceptances": ACCEPTANCES, "invoices": [INVOICE]})
    page = client.get("/batches").text
    assert 'name="version"' in page and 'name="expected_balance"' in page
    assert 'value="300.00"' in page  # remaining USD balance on GR-2


def test_page_batch_accepts_real_entries(client):
    # Simulates the form: existing acceptance carries its version, new rows are blank.
    first = client.post("/api/reconcile", json={
        "acceptances": [{**ACCEPTANCES[0], "expected_balance": "1000.00", "version": 0}],
        "invoices": [{**INVOICE, "original_amount": "400.00"}],
    })
    assert first.status_code == 200
    second = client.post("/api/reconcile", json={
        "acceptances": [
            {**ACCEPTANCES[0], "expected_balance": "600.00", "version": 1},
            {**ACCEPTANCES[1], "expected_balance": "500.00", "version": 0},
        ],
        "invoices": [{**INVOICE, "invoice_id": "IV-2#1", "doc_no": "IV-2", "original_amount": "300.00"}],
    })
    assert second.status_code == 200
    data = second.json()
    assert data["groups"][0]["written_off"] == "700.00"
    assert data["groups"][0]["uninvoiced"] == "800.00"

