"""
Read-only JSON API over the Stock Tracker SQLite database.

Runs alongside the existing Streamlit app (app.py) and systemd service
(stockapp.service), reading the same stock_reports.db file directly.
Does not modify any data -- GET endpoints only, no writes.

Configure the data directory with the STOCKAPP_DATA_DIR environment
variable (must match what app.py / stockapp.service use), same
convention as app.py.
"""

import os
import sqlite3

from fastapi import FastAPI, HTTPException, Query

import update_quotes as uq

DATA_DIR = os.environ.get("STOCKAPP_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
uq.DB_PATH = os.path.join(DATA_DIR, "stock_reports.db")

app = FastAPI(title="Stock Tracker Query API", version="1.0")


def get_conn():
    if not os.path.exists(uq.DB_PATH):
        raise HTTPException(status_code=503, detail=f"Database not found at {uq.DB_PATH}")
    conn = sqlite3.connect(uq.DB_PATH)
    uq.init_db(conn)
    return conn


# --------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------

QUOTE_COLUMNS = [
    "generated_at", "current_price", "previous_close", "pe_ratio", "dividend_yield",
    "day_high", "day_low", "week52_high", "week52_low", "volume", "market_cap", "error_message",
]


@app.get("/quote/{symbol}")
def get_quote_history(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    latest_only: bool = False,
):
    """Quote history for one symbol, newest first. Set latest_only=true
    for just the most recent quote."""
    conn = get_conn()
    try:
        rows = uq.get_symbol_quote_history(conn, symbol, start_date=start_date, end_date=end_date)
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail=f"No quote data found for symbol '{symbol.upper()}'")

    history = [dict(zip(QUOTE_COLUMNS, row)) for row in rows]
    if latest_only:
        return {"symbol": symbol.upper(), "quote": history[0]}
    return {"symbol": symbol.upper(), "count": len(history), "history": history}


# --------------------------------------------------------------------
# Portfolios
# --------------------------------------------------------------------

@app.get("/portfolios")
def get_portfolios():
    """List every portfolio: id, name, owner."""
    conn = get_conn()
    try:
        rows = uq.list_portfolios(conn)
    finally:
        conn.close()
    return [{"id": pid, "name": name, "owner": owner} for pid, name, owner in rows]


@app.get("/portfolio/{portfolio_id}/positions")
def get_positions(portfolio_id: int):
    """Current holdings (non-zero positions) for one portfolio."""
    conn = get_conn()
    try:
        # Confirm the portfolio actually exists, so a bad ID gets a 404
        # instead of a silent empty list.
        exists = conn.execute("SELECT 1 FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail=f"No portfolio with id {portfolio_id}")
        positions = uq.get_portfolio_positions(conn, portfolio_id)
    finally:
        conn.close()
    return {"portfolio_id": portfolio_id, "positions": positions}


# --------------------------------------------------------------------
# Value screens
# --------------------------------------------------------------------

VALUE_SCREEN_COLUMNS = [
    "symbol", "name", "group_name", "pe_ratio", "peer_avg_pe", "peer_count",
    "pct_of_52wk_range", "dividend_yield", "current_price", "market_cap",
]

DEEP_VALUE_SCREEN_COLUMNS = [
    "symbol", "name", "sector", "current_price", "market_cap", "pe_ratio",
    "price_to_book", "return_on_equity", "debt_to_equity", "peg_ratio",
    "profit_margins", "revenue_growth",
]


@app.get("/screen/value")
def value_screen(
    group_by: str = Query("sector", pattern="^(sector|industry)$"),
    scope: str | None = None,
    min_pe: float = 3.0,
    max_pe: float = 60.0,
    min_price: float = 5.0,
    min_market_cap: float = 1_000_000_000.0,
    limit: int = 25,
):
    """P/E value screen: candidates trading below their sector/industry
    peer-group average P/E."""
    conn = get_conn()
    try:
        rows = uq.run_value_screen(
            conn, group_by=group_by, scope=scope, min_pe=min_pe, max_pe=max_pe,
            min_price=min_price, min_market_cap=min_market_cap, limit=limit,
        )
    finally:
        conn.close()
    return {"count": len(rows), "results": [dict(zip(VALUE_SCREEN_COLUMNS, row)) for row in rows]}


@app.get("/screen/deep-value")
def deep_value_screen(
    max_pb: float = 3.0,
    min_roe: float = 0.10,
    max_debt_equity: float = 150.0,
    max_peg: float = 2.0,
    min_price: float = 5.0,
    min_market_cap: float = 1_000_000_000.0,
    limit: int = 25,
):
    """Fundamentals-based deep value screen: low P/B, strong ROE,
    manageable debt, reasonable PEG."""
    conn = get_conn()
    try:
        rows = uq.run_deep_value_screen(
            conn, max_pb=max_pb, min_roe=min_roe, max_debt_equity=max_debt_equity,
            max_peg=max_peg, min_price=min_price, min_market_cap=min_market_cap, limit=limit,
        )
    finally:
        conn.close()
    return {"count": len(rows), "results": [dict(zip(DEEP_VALUE_SCREEN_COLUMNS, row)) for row in rows]}


@app.get("/health")
def health():
    return {"status": "ok", "db_path": uq.DB_PATH, "db_exists": os.path.exists(uq.DB_PATH)}
