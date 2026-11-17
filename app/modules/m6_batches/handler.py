import json as jsonlib
import sqlite3
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.web import page
from app.errors import DomainError
from . import fx
from .service import run, differences_for_export, result_for
from .exporter import differences_csv

router = APIRouter()
FALLBACK_ROWS = [{"key": "RUN-659-A", "status": "守恒", "value": "36", "currency": "CNY"}]


def _template_rows(data: dict) -> list[dict]:
    rows = []
    for row in data["groups"]:
        rows.append({
            "sku": row["sku"],
            "currency": row["currency"],
            "received": row["received"],
            "written_off": row["written_off"],
            "uninvoiced": row["uninvoiced"],
            "cost_cny": row["cost_cny"],
            "payable_cny": row["payable_cny"],
            "fx_difference_cny": row["fx_difference_cny"],
            "status": "未开票" if Decimal(row["uninvoiced"]) > 0 else "已核销",
        })
    return rows


def _page_context(data: dict | None, result: dict | None = None) -> dict:
    if data is None:
        data = {"acceptances": [], "invoices": [], "groups": [], "currency_totals": [],
                "differences": [], "balanced": True, "run_id": ""}
    open_amounts = [Decimal(row["open_amount"]) for row in data["acceptances"]]
    fx_total = sum((Decimal(row["fx_difference_cny"]) for row in data["groups"]), Decimal("0"))
    fully_written = all(amount == 0 for amount in open_amounts)
    return {
        "acceptances": data["acceptances"],
        "invoices": data["invoices"],
        "groups": _template_rows(data),
        "currency_totals": data["currency_totals"],
        "differences": data["differences"],
        "balanced": data["balanced"],
        "fully_written": fully_written,
        "run_id": data.get("run_id", ""),
        "result_json": jsonlib.dumps(result, ensure_ascii=False, indent=2) if result else "",
        "summary": {
            "acceptances": len(data["acceptances"]),
            "invoices": len(data["invoices"]),
            "differences": len(data["differences"]),
            "open_original": fx.amount_text(sum(open_amounts, Decimal("0"))) if open_amounts else "0.00",
            "fx_difference": fx.plain(fx.money(fx_total)),
        },
    }


@router.get("/batches", response_class=HTMLResponse)
def view(request: Request):
    from app import store

    try:
        with store.snapshot() as db:
            data = result_for(db, "", None)
            latest = db.execute("SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
            if latest:
                data["run_id"] = latest[0]
    except (DomainError, sqlite3.Error):
        data = None
    return page(request, "m6.html", "差异批次", "/api/reconcile", _template_rows(data) if data else FALLBACK_ROWS,
                **_page_context(data))


@router.post("/api/reconcile")
def execute(payload: dict | None = None):
    try:
        return run(payload)
    except DomainError as exc:
        return _error(exc)


@router.get("/api/export/differences.csv")
def export_differences():
    body = differences_csv(differences_for_export())
    return PlainTextResponse(
        body, media_type="text/csv", headers={"content-disposition": "attachment; filename=differences.csv"}
    )


def _error(exc: DomainError):
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )
