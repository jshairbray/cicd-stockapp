"""
JSON API for update_quotes.py, running alongside the Streamlit GUI on the
same VM. Reuses the exact same functions/database — this is not a rewrite,
just a second, machine-friendly way to call the same code the web GUI does.

Auth: every request needs an `Authorization: Bearer <token>` header, checked
against the API_TOKEN environment variable (see api.service).
"""
import contextlib
import hmac
import io
import os
import sqlite3
from datetime import date

from flask import Flask, jsonify, request

import update_quotes as uq

DATA_DIR = os.environ.get("STOCKAPP_DATA_DIR", "/opt/stockapp/data")
uq.OUTPUT_DIR = DATA_DIR + "/"
uq.DB_PATH = os.path.join(DATA_DIR, "stock_reports.db")
uq.PDF_OUTPUT_DIR = os.path.join(DATA_DIR, "pdfs") + "/"

API_TOKEN = os.environ["API_TOKEN"]

app = Flask(__name__)


def get_conn():
    conn = sqlite3.connect(uq.DB_PATH)
    uq.init_db(conn)
    return conn


def run_captured(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


@app.before_request
def check_auth():
    if request.path == "/api/health":
        return
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not hmac.compare_digest(token, API_TOKEN):
        return jsonify({"error": "unauthorized"}), 401


def find_portfolio(conn, name):
    for pid, pname, owner in uq.list_portfolios(conn):
        if pname.lower() == name.lower():
            return pid, pname, owner
    return None, None, None


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/portfolios")
def api_portfolios():
    conn = get_conn()
    rows = uq.list_portfolios(conn)
    conn.close()
    return jsonify([{"id": pid, "name": name, "owner": owner} for pid, name, owner in rows])


@app.route("/api/portfolio/<name>")
def api_portfolio(name):
    conn = get_conn()
    pid, pname, owner = find_portfolio(conn, name)
    if pid is None:
        conn.close()
        return jsonify({"error": f"no portfolio named '{name}'"}), 404
    _, output = run_captured(uq.print_portfolio, conn, pid)
    conn.close()
    return jsonify({"portfolio": pname, "owner": owner, "output": output})


@app.route("/api/portfolio/<name>/transactions")
def api_transactions(name):
    conn = get_conn()
    pid, pname, owner = find_portfolio(conn, name)
    if pid is None:
        conn.close()
        return jsonify({"error": f"no portfolio named '{name}'"}), 404
    rows = uq.list_transactions(conn, pid)
    conn.close()
    headers = ["id", "date", "type", "symbol", "quantity", "price_per_share", "latest_price"]
    return jsonify([dict(zip(headers, r)) for r in rows])


@app.route("/api/buy", methods=["POST"])
def api_buy():
    data = request.get_json(force=True)
    conn = get_conn()
    pid, pname, owner = find_portfolio(conn, data["portfolio"])
    if pid is None:
        conn.close()
        return jsonify({"error": f"no portfolio named '{data['portfolio']}'"}), 404
    txn_date = data.get("date") or str(date.today())
    uq.record_transaction(conn, pid, data["symbol"].upper(), "BUY", txn_date, float(data["quantity"]), float(data["price"]))
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/sell", methods=["POST"])
def api_sell():
    data = request.get_json(force=True)
    conn = get_conn()
    pid, pname, owner = find_portfolio(conn, data["portfolio"])
    if pid is None:
        conn.close()
        return jsonify({"error": f"no portfolio named '{data['portfolio']}'"}), 404
    txn_date = data.get("date") or str(date.today())
    uq.record_transaction(conn, pid, data["symbol"].upper(), "SELL", txn_date, float(data["quantity"]), float(data["price"]))
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/groups")
def api_groups():
    conn = get_conn()
    rows = uq.list_groups(conn)
    conn.close()
    return jsonify([{"name": name, "count": count} for name, count in rows])


@app.route("/api/group/<name>")
def api_group(name):
    conn = get_conn()
    _, output = run_captured(uq.print_group_quotes, conn, name)
    conn.close()
    return jsonify({"group": name, "output": output})


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(force=True) if request.data else {}
    conn = get_conn()
    if "symbols" in data:
        symbols = [s.upper() for s in data["symbols"]]
    elif "group" in data:
        symbols = uq.get_group_symbols(conn, data["group"])
    elif "sector" in data:
        symbols = uq.get_symbols_by_sector(conn, data["sector"])
    elif "industry" in data:
        symbols = uq.get_symbols_by_industry(conn, data["industry"])
    elif "exchange" in data:
        symbols = uq.get_symbols_by_exchange(conn, data["exchange"])
    else:
        symbols = uq.get_tracked_symbols(conn)
    conn.close()
    if not symbols:
        return jsonify({"error": "no matching symbols found"}), 400
    _, output = run_captured(uq.fetch_stock_data, symbols)
    return jsonify({"symbols": symbols, "output": output})


@app.route("/api/company/<symbol>")
def api_company(symbol):
    conn = get_conn()
    _, output = run_captured(uq.print_company, conn, symbol.upper())
    conn.close()
    return jsonify({"symbol": symbol.upper(), "output": output})


@app.route("/api/symbol/<symbol>")
def api_symbol(symbol):
    full_history = request.args.get("full_history", "false").lower() == "true"
    conn = get_conn()
    _, output = run_captured(uq.print_symbol_quotes, conn, symbol.upper(), full_history=full_history)
    conn.close()
    return jsonify({"symbol": symbol.upper(), "output": output})


@app.route("/api/fundamentals/<symbol>")
def api_fundamentals(symbol):
    full_history = request.args.get("full_history", "false").lower() == "true"
    conn = get_conn()
    _, output = run_captured(uq.print_company_fundamentals, conn, symbol.upper(), full_history=full_history)
    conn.close()
    return jsonify({"symbol": symbol.upper(), "output": output})


@app.route("/api/value-screen")
def api_value_screen():
    group_by = request.args.get("group_by", "sector")
    scope = request.args.get("scope")
    min_pe = float(request.args.get("min_pe", 3.0))
    max_pe = float(request.args.get("max_pe", 60.0))
    min_price = float(request.args.get("min_price", 5.0))
    min_market_cap = float(request.args.get("min_market_cap", 1_000_000_000))
    limit = int(request.args.get("limit", 25))
    conn = get_conn()
    rows = uq.run_value_screen(
        conn, group_by=group_by, scope=scope, min_pe=min_pe, max_pe=max_pe,
        min_price=min_price, min_market_cap=min_market_cap, limit=limit,
    )
    _, output = run_captured(uq.print_value_screen, rows, group_by=group_by)
    conn.close()
    return jsonify({"output": output})


@app.route("/api/deep-value-screen")
def api_deep_value_screen():
    max_pb = float(request.args.get("max_pb", 3.0))
    min_roe = float(request.args.get("min_roe", 0.10))
    max_debt_equity = float(request.args.get("max_debt_equity", 150.0))
    max_peg = float(request.args.get("max_peg", 2.0))
    conn = get_conn()
    rows = uq.run_deep_value_screen(
        conn, max_pb=max_pb, min_roe=min_roe, max_debt_equity=max_debt_equity, max_peg=max_peg,
    )
    _, output = run_captured(uq.print_deep_value_screen, rows)
    conn.close()
    return jsonify({"output": output})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("API_PORT", 8503)))
