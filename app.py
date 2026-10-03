"""
Web GUI for update_quotes.py / generate_docs.py.

This does NOT reimplement any of the stock-tracking logic. It imports
the two scripts as modules, points their DB_PATH/OUTPUT_DIR/
PDF_OUTPUT_DIR constants at a cloud-friendly data directory (instead of
the original /sdcard/... Android paths), and calls their existing
functions directly -- capturing whatever they normally print to the
console and rendering it in the browser instead.

Configure the data directory with the STOCKAPP_DATA_DIR environment
variable (defaults to ./data next to this file).
"""

import contextlib
import io
import os
import sqlite3
from datetime import date

import streamlit as st

import update_quotes as uq
import generate_docs as gd

# --------------------------------------------------------------------
# Redirect the scripts' storage from /sdcard/... to a configurable,
# cloud-friendly directory. Must happen before any DB/PDF calls.
# --------------------------------------------------------------------
DATA_DIR = os.environ.get("STOCKAPP_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
PDF_DIR = os.path.join(DATA_DIR, "pdfs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)

uq.OUTPUT_DIR = DATA_DIR + os.sep
uq.DB_PATH = os.path.join(DATA_DIR, "stock_reports.db")
uq.PDF_OUTPUT_DIR = PDF_DIR + os.sep
gd.OUTPUT_DIR = DATA_DIR + os.sep
gd.OUTPUT_PATH = os.path.join(DATA_DIR, "update_quotes_documentation.pdf")

st.set_page_config(page_title="Stock Tracker", layout="wide")


def get_conn():
    conn = sqlite3.connect(uq.DB_PATH)
    uq.init_db(conn)
    return conn


def run_captured(fn, *args, **kwargs):
    """Call fn, capturing whatever it prints, and return (result, output_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def show_output(text):
    if text.strip():
        st.code(text, language=None)


st.title("📈 Stock Tracker")
st.caption(f"Database: {uq.DB_PATH}")

page = st.sidebar.radio(
    "Section",
    [
        "Dashboard",
        "Fetch Quotes",
        "Portfolios",
        "Groups",
        "Value Screens",
        "Company Lookup",
        "Bulk Import",
        "Documentation PDF",
    ],
)

# ----------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------
if page == "Dashboard":
    conn = get_conn()
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Portfolios")
        portfolios = uq.list_portfolios(conn)
        if portfolios:
            st.table([{"Portfolio": name, "Owner": owner} for _, name, owner in portfolios])
        else:
            st.info("No portfolios yet -- create one under the Portfolios tab.")

    with col2:
        st.subheader("Groups")
        groups = uq.list_groups(conn)
        if groups:
            st.table([{"Group": name, "Symbols": count} for name, count in groups])
        else:
            st.info("No groups yet -- create one under the Groups tab.")

    tracked = uq.get_tracked_symbols(conn)
    st.metric("Symbols tracked", len(tracked))
    conn.close()

# ----------------------------------------------------------------
# Fetch Quotes
# ----------------------------------------------------------------
elif page == "Fetch Quotes":
    st.subheader("Fetch latest quotes from Yahoo Finance")
    st.caption("Pick exactly one way to choose symbols, or leave everything blank to refresh every symbol ever tracked.")

    mode = st.radio(
        "Fetch by",
        ["Everything tracked", "Specific symbols", "Group", "Sector", "Industry", "Exchange", "Portfolio"],
        horizontal=True,
    )

    conn = get_conn()
    symbols = []

    if mode == "Specific symbols":
        raw = st.text_input("Symbols (space or comma separated)", "AAPL MSFT GOOGL")
        symbols = [s.strip().upper() for s in raw.replace(",", " ").split() if s.strip()]
    elif mode == "Group":
        groups = [g[0] for g in uq.list_groups(conn)]
        choice = st.selectbox("Group", groups) if groups else None
        if choice:
            symbols = uq.get_group_symbols(conn, choice)
    elif mode == "Sector":
        sector = st.text_input("Sector name", "Healthcare")
        symbols = uq.get_symbols_by_sector(conn, sector) if sector else []
    elif mode == "Industry":
        industry = st.text_input("Industry name", "")
        symbols = uq.get_symbols_by_industry(conn, industry) if industry else []
    elif mode == "Exchange":
        exchange = st.text_input("Exchange name", "NASDAQ")
        symbols = uq.get_symbols_by_exchange(conn, exchange) if exchange else []
    elif mode == "Portfolio":
        portfolios = uq.list_portfolios(conn)
        labels = {f"{name} ({owner})": pid for pid, name, owner in portfolios}
        chosen = st.multiselect("Portfolio(s)", list(labels.keys()))
        seen = set()
        for c in chosen:
            for s in uq.get_symbols_by_portfolio_id(conn, labels[c]):
                if s not in seen:
                    seen.add(s)
                    symbols.append(s)
    else:
        symbols = uq.get_tracked_symbols(conn)

    conn.close()

    if symbols:
        st.write(f"{len(symbols)} symbol(s): {', '.join(symbols)}")
    if st.button("Fetch now", type="primary", disabled=not symbols):
        with st.spinner(f"Fetching {len(symbols)} symbol(s) from Yahoo Finance..."):
            _, output = run_captured(uq.fetch_stock_data, symbols)
        st.success("Done.")
        show_output(output)

# ----------------------------------------------------------------
# Portfolios
# ----------------------------------------------------------------
elif page == "Portfolios":
    conn = get_conn()
    tab_view, tab_txn, tab_new = st.tabs(["View / Buy / Sell", "Transaction Log", "New Portfolio"])

    portfolios = uq.list_portfolios(conn)
    labels = {f"{name} ({owner})": (pid, name, owner) for pid, name, owner in portfolios}

    with tab_view:
        if not labels:
            st.info("No portfolios yet -- create one under 'New Portfolio'.")
        else:
            choice = st.selectbox("Portfolio", list(labels.keys()))
            pid, pname, powner = labels[choice]

            _, output = run_captured(uq.print_portfolio, conn, pid)
            show_output(output)

            st.markdown("---")
            bcol, scol = st.columns(2)
            with bcol:
                st.markdown("**Buy**")
                with st.form("buy_form"):
                    b_symbol = st.text_input("Symbol", key="buy_symbol").upper()
                    b_qty = st.number_input("Quantity", min_value=0.0, step=1.0, key="buy_qty")
                    b_price = st.number_input("Price per share", min_value=0.0, step=0.01, key="buy_price")
                    b_date = st.date_input("Date", value=date.today(), key="buy_date")
                    if st.form_submit_button("Record buy") and b_symbol and b_qty > 0:
                        uq.record_transaction(conn, pid, b_symbol, "BUY", str(b_date), b_qty, b_price)
                        st.success(f"Bought {b_qty} {b_symbol} @ {b_price}")
                        st.rerun()
            with scol:
                st.markdown("**Sell**")
                with st.form("sell_form"):
                    s_symbol = st.text_input("Symbol", key="sell_symbol").upper()
                    s_qty = st.number_input("Quantity", min_value=0.0, step=1.0, key="sell_qty")
                    s_price = st.number_input("Price per share", min_value=0.0, step=0.01, key="sell_price")
                    s_date = st.date_input("Date", value=date.today(), key="sell_date")
                    if st.form_submit_button("Record sell") and s_symbol and s_qty > 0:
                        uq.record_transaction(conn, pid, s_symbol, "SELL", str(s_date), s_qty, s_price)
                        st.success(f"Sold {s_qty} {s_symbol} @ {s_price}")
                        st.rerun()

            st.markdown("---")
            if st.button("🗑 Delete this portfolio"):
                uq.delete_portfolio(conn, pid)
                st.success(f"Deleted {pname}.")
                st.rerun()

    with tab_txn:
        if not labels:
            st.info("No portfolios yet.")
        else:
            choice = st.selectbox("Portfolio", list(labels.keys()), key="txn_portfolio")
            pid, pname, powner = labels[choice]
            rows = uq.list_transactions(conn, pid)
            if rows:
                headers = ["id", "date", "type", "symbol", "quantity", "price_per_share", "latest_price"]
                st.dataframe([dict(zip(headers, r)) for r in rows], hide_index=True)
                del_id = st.number_input("Transaction ID to delete", min_value=0, step=1)
                if st.button("Delete transaction") and del_id:
                    uq.delete_transaction(conn, int(del_id))
                    st.success(f"Deleted transaction {del_id}.")
                    st.rerun()
            else:
                st.info("No transactions logged yet for this portfolio.")

    with tab_new:
        with st.form("new_portfolio_form"):
            name = st.text_input("Portfolio name")
            owner = st.selectbox("Owner", uq.PORTFOLIO_OWNERS)
            if st.form_submit_button("Create portfolio") and name:
                uq.get_or_create_portfolio(conn, name, owner)
                st.success(f"Created '{name}' for {owner}.")
                st.rerun()

    conn.close()

# ----------------------------------------------------------------
# Groups
# ----------------------------------------------------------------
elif page == "Groups":
    conn = get_conn()
    tab_view, tab_manage, tab_seed = st.tabs(["View", "Add / Remove Symbols", "Seed Built-ins"])

    with tab_view:
        groups = [g[0] for g in uq.list_groups(conn)]
        if groups:
            choice = st.selectbox("Group", groups)
            _, output = run_captured(uq.print_group_quotes, conn, choice)
            show_output(output)
        else:
            st.info("No groups yet.")

    with tab_manage:
        with st.form("group_form"):
            gname = st.text_input("Group name (existing or new)")
            raw = st.text_input("Symbols (space or comma separated)")
            syms = [s.strip().upper() for s in raw.replace(",", " ").split() if s.strip()]
            c1, c2 = st.columns(2)
            add_clicked = c1.form_submit_button("Add to group")
            remove_clicked = c2.form_submit_button("Remove from group")
            if add_clicked and gname and syms:
                n = uq.add_symbols_to_group(conn, gname, syms)
                st.success(f"Added {n} symbol(s) to '{gname}'.")
                st.rerun()
            if remove_clicked and gname and syms:
                n = uq.remove_symbols_from_group(conn, gname, syms)
                st.success(f"Removed {n} symbol(s) from '{gname}'.")
                st.rerun()

    with tab_seed:
        st.write("Populate built-in groups.")
        c1, c2 = st.columns(2)
        if c1.button("Seed Dow 30"):
            _, output = run_captured(uq.seed_dow30, conn)
            show_output(output)
        if c2.button("Seed S&P 500 (downloads a CSV)"):
            with st.spinner("Downloading constituent list..."):
                _, output = run_captured(uq.seed_sp500, conn)
            show_output(output)

    conn.close()

# ----------------------------------------------------------------
# Value Screens
# ----------------------------------------------------------------
elif page == "Value Screens":
    conn = get_conn()
    tab_pe, tab_deep = st.tabs(["P/E Value Screen", "Deep Value Screen (fundamentals)"])

    with tab_pe:
        c1, c2, c3 = st.columns(3)
        group_by = c1.selectbox("Group by", ["sector", "industry"])
        scope = c2.text_input("Scope (optional)", "")
        limit = c3.number_input("Limit", min_value=1, value=25)
        c4, c5, c6, c7 = st.columns(4)
        min_pe = c4.number_input("Min P/E", value=3.0)
        max_pe = c5.number_input("Max P/E", value=60.0)
        min_price = c6.number_input("Min price", value=5.0)
        min_mcap = c7.number_input("Min market cap", value=1_000_000_000.0, format="%.0f")
        if st.button("Run value screen"):
            rows = uq.run_value_screen(
                conn, group_by=group_by, scope=scope or None, min_pe=min_pe, max_pe=max_pe,
                min_price=min_price, min_market_cap=min_mcap, limit=int(limit),
            )
            _, output = run_captured(uq.print_value_screen, rows, group_by=group_by)
            show_output(output)

    with tab_deep:
        c1, c2, c3, c4 = st.columns(4)
        max_pb = c1.number_input("Max P/B", value=3.0)
        min_roe = c2.number_input("Min ROE (fraction)", value=0.10)
        max_de = c3.number_input("Max debt/equity", value=150.0)
        max_peg = c4.number_input("Max PEG", value=2.0)
        if st.button("Run deep value screen"):
            rows = uq.run_deep_value_screen(
                conn, max_pb=max_pb, min_roe=min_roe, max_debt_equity=max_de, max_peg=max_peg,
            )
            _, output = run_captured(uq.print_deep_value_screen, rows)
            show_output(output)

    conn.close()

# ----------------------------------------------------------------
# Company Lookup
# ----------------------------------------------------------------
elif page == "Company Lookup":
    conn = get_conn()
    symbol = st.text_input("Symbol", "AAPL").upper()
    tab_co, tab_quotes, tab_fund = st.tabs(["Company Info", "Quote History", "Fundamentals History"])

    with tab_co:
        if st.button("Show company") and symbol:
            _, output = run_captured(uq.print_company, conn, symbol)
            show_output(output)

    with tab_quotes:
        full_history = st.checkbox("Full history", key="q_full")
        if st.button("Show quotes") and symbol:
            _, output = run_captured(uq.print_symbol_quotes, conn, symbol, full_history=full_history)
            show_output(output)

    with tab_fund:
        full_history_f = st.checkbox("Full history", key="f_full")
        if st.button("Show fundamentals") and symbol:
            _, output = run_captured(uq.print_company_fundamentals, conn, symbol, full_history=full_history_f)
            show_output(output)

    conn.close()

# ----------------------------------------------------------------
# Bulk Import
# ----------------------------------------------------------------
elif page == "Bulk Import":
    st.subheader("Bulk import transactions / companies / groups from a CSV")
    st.caption(
        "One action per row: BUY, SELL, EDIT_TRANSACTION, DELETE_TRANSACTION, "
        "DELETE_PORTFOLIO, WATCHLIST, REMOVE_FROM_GROUP, ADD_COMPANY, EDIT_COMPANY, DELETE_COMPANY."
    )

    template_path = os.path.join(DATA_DIR, "import_template.csv")
    if st.button("Generate starter template"):
        uq.write_import_template(template_path)
    if os.path.exists(template_path):
        with open(template_path, "rb") as f:
            st.download_button("Download template CSV", f, file_name="import_template.csv")

    st.markdown("---")
    uploaded = st.file_uploader("Upload your filled-in CSV", type=["csv"])
    if uploaded is not None:
        st.write(f"**{uploaded.name}** ({uploaded.size} bytes)")
        if st.button("Run import", type="primary"):
            tmp_path = os.path.join(DATA_DIR, "_pending_import.csv")
            with open(tmp_path, "wb") as f:
                f.write(uploaded.getvalue())

            conn = get_conn()
            successes, errors = uq.process_import_file(conn, tmp_path)
            conn.close()
            os.remove(tmp_path)

            st.success(f"{successes} row(s) applied successfully.")
            if errors:
                st.error(f"{len(errors)} issue(s):")
                for e in errors:
                    st.write(f"- {e}")

# ----------------------------------------------------------------
# Documentation PDF
# ----------------------------------------------------------------
elif page == "Documentation PDF":
    st.write("Regenerate the update_quotes.py feature documentation as a PDF.")
    if st.button("Generate PDF"):
        gd.build_pdf()
        st.success("PDF generated.")
    if os.path.exists(gd.OUTPUT_PATH):
        with open(gd.OUTPUT_PATH, "rb") as f:
            st.download_button("Download documentation PDF", f, file_name="update_quotes_documentation.pdf")
