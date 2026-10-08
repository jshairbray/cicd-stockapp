import os
import csv
import sqlite3
import time
import contextlib
from datetime import datetime

import yfinance as yf

OUTPUT_DIR = "/sdcard/Download/"
DB_PATH = os.path.join(OUTPUT_DIR, "stock_reports.db")

# Where --pdf reports are saved. Every PDF filename gets a
# YYYYMMDDHHMMSS timestamp appended automatically (see
# build_pdf_output_path), so repeated exports never overwrite each other.
PDF_OUTPUT_DIR = "/sdcard/documents/"

# Set to True to also write a .txt report file to OUTPUT_DIR on each run,
# in addition to the database. Currently disabled -- only the database
# gets written to.
WRITE_TXT_REPORT = False

# Yahoo Finance (via yfinance) occasionally returns transient errors,
# especially under back-to-back requests. These control how the script
# retries a failed symbol before giving up, and how long it pauses
# between symbols to avoid triggering rate limits in the first place.
MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 2   # doubles each retry: 2s, 4s, 8s
REQUEST_DELAY_SECONDS = 1      # pause between symbols


# --------------------------------------------------------------------------
# Database schema
#
# Normalized to 3NF:
#   - industries / sectors: deduplicated lookup tables (many companies
#     share the same industry/sector, so these are stored once and
#     referenced by id rather than repeating text everywhere).
#   - companies: static/slow-changing attributes of a symbol (name,
#     industry, sector). One row per symbol, not duplicated per report.
#   - reports: one row per script run, so every quote can be traced back
#     to when it was fetched.
#   - quotes: the volatile, time-varying data (price, volume, ratios).
#     One row per (report, company) pair. This is what changes on every
#     run, so it's kept separate from the static company data instead of
#     re-storing name/industry/sector every time.
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS industries (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS sectors (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS exchanges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    exchange_name TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL UNIQUE,
    name         TEXT,
    industry_id  INTEGER REFERENCES industries(id),
    sector_id    INTEGER REFERENCES sectors(id),
    exchange_id  INTEGER REFERENCES exchanges(id)
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id         INTEGER NOT NULL REFERENCES reports(id),
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    pe_ratio          REAL,
    dividend_yield    REAL,
    current_price     REAL,
    previous_close    REAL,
    day_high          REAL,
    day_low           REAL,
    week52_high       REAL,
    week52_low        REAL,
    volume            INTEGER,
    market_cap        REAL,
    error_message     TEXT,
    UNIQUE(report_id, company_id)
);

-- Groups let you tag companies into named sets (Portfolio, Dow 30,
-- S&P 500, or any custom watchlist) independent of sector/industry/
-- exchange, which are already derivable from the companies table.
CREATE TABLE IF NOT EXISTS groups (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id   INTEGER NOT NULL REFERENCES groups(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    PRIMARY KEY (group_id, company_id)
);

-- Portfolios: any number of them, each tied to an owner. Holdings track
-- the current position per (portfolio, stock); transactions are the
-- buy/sell log. Holdings are never edited directly -- they're kept in
-- sync automatically by the triggers below whenever a transaction is
-- inserted, so the transaction log is always the source of truth.
CREATE TABLE IF NOT EXISTS portfolio_owners (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS portfolios (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES portfolio_owners(id),
    UNIQUE(name, owner_id)
);

CREATE TABLE IF NOT EXISTS holdings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id         INTEGER NOT NULL REFERENCES portfolios(id),
    company_id           INTEGER NOT NULL REFERENCES companies(id),
    quantity             REAL NOT NULL DEFAULT 0,
    avg_purchase_price   REAL,
    first_purchase_date  TEXT,
    UNIQUE(portfolio_id, company_id)
);

CREATE TABLE IF NOT EXISTS portfolio_transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id      INTEGER NOT NULL REFERENCES portfolios(id),
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    transaction_type  TEXT NOT NULL CHECK(transaction_type IN ('BUY','SELL')),
    transaction_date  TEXT NOT NULL,
    quantity          REAL NOT NULL,
    price_per_share   REAL NOT NULL
);

-- Fundamentals: a second time-series table, separate from quotes, for
-- deeper valuation/health metrics (book value, returns, margins, cash
-- flow, growth, etc). Kept apart from quotes because these are a
-- different category of data (company financial-statement-derived
-- metrics vs. live market quote data) and not every consumer of quotes
-- needs them. Same one-row-per-(report,company) shape as quotes, so
-- it can be joined the same way.
CREATE TABLE IF NOT EXISTS fundamentals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id           INTEGER NOT NULL REFERENCES reports(id),
    company_id          INTEGER NOT NULL REFERENCES companies(id),
    price_to_book       REAL,
    book_value          REAL,
    return_on_equity    REAL,
    debt_to_equity      REAL,
    current_ratio       REAL,
    quick_ratio         REAL,
    revenue_growth      REAL,
    earnings_growth     REAL,
    free_cashflow       REAL,
    operating_cashflow  REAL,
    profit_margins      REAL,
    gross_margins       REAL,
    operating_margins   REAL,
    return_on_assets    REAL,
    enterprise_value    REAL,
    peg_ratio           REAL,
    forward_pe          REAL,
    trailing_pe         REAL,
    UNIQUE(report_id, company_id)
);
"""

# Triggers and views can't use IF NOT EXISTS reliably alongside
# executescript's transaction handling in older SQLite the same way
# tables can, so they're created separately after the main schema.
SCHEMA_TRIGGERS_AND_VIEWS = """
DROP TRIGGER IF EXISTS trg_txn_buy_upsert;
CREATE TRIGGER trg_txn_buy_upsert
AFTER INSERT ON portfolio_transactions
WHEN NEW.transaction_type = 'BUY'
BEGIN
    INSERT INTO holdings (portfolio_id, company_id, quantity, avg_purchase_price, first_purchase_date)
    VALUES (NEW.portfolio_id, NEW.company_id, NEW.quantity, NEW.price_per_share, NEW.transaction_date)
    ON CONFLICT(portfolio_id, company_id) DO UPDATE SET
        avg_purchase_price = ((holdings.quantity * holdings.avg_purchase_price) + (NEW.quantity * NEW.price_per_share))
                              / (holdings.quantity + NEW.quantity),
        quantity = holdings.quantity + NEW.quantity;
END;

DROP TRIGGER IF EXISTS trg_txn_sell_update;
CREATE TRIGGER trg_txn_sell_update
AFTER INSERT ON portfolio_transactions
WHEN NEW.transaction_type = 'SELL'
BEGIN
    UPDATE holdings
    SET quantity = quantity - NEW.quantity
    WHERE portfolio_id = NEW.portfolio_id AND company_id = NEW.company_id;
END;

DROP VIEW IF EXISTS portfolio_positions;
CREATE VIEW portfolio_positions AS
SELECT
    p.id AS portfolio_id,
    p.name AS portfolio_name,
    o.name AS owner,
    c.symbol,
    c.name AS company_name,
    h.quantity,
    h.avg_purchase_price,
    h.first_purchase_date,
    lq.current_price,
    lq.previous_close,
    ROUND(lq.current_price - lq.previous_close, 2) AS day_change,
    ROUND((lq.current_price - lq.previous_close) / lq.previous_close * 100, 2) AS day_change_pct,
    lq.generated_at AS quote_as_of,
    ROUND((lq.current_price - h.avg_purchase_price) / h.avg_purchase_price * 100, 2) AS pct_gain_loss,
    ROUND(h.quantity * lq.current_price, 2) AS position_value
FROM holdings h
JOIN portfolios p ON p.id = h.portfolio_id
JOIN portfolio_owners o ON o.id = p.owner_id
JOIN companies c ON c.id = h.company_id
LEFT JOIN (
    SELECT q.company_id, q.current_price, q.previous_close, r.generated_at
    FROM quotes q
    JOIN reports r ON r.id = q.report_id
    JOIN (SELECT company_id, MAX(report_id) AS max_report FROM quotes GROUP BY company_id) latest
      ON latest.company_id = q.company_id AND latest.max_report = q.report_id
) lq ON lq.company_id = h.company_id;

DROP VIEW IF EXISTS portfolio_totals;
CREATE VIEW portfolio_totals AS
SELECT portfolio_id, portfolio_name, owner, SUM(position_value) AS total_portfolio_value
FROM portfolio_positions
GROUP BY portfolio_id;
"""

PORTFOLIO_OWNERS = [
    "Jonathan", "Braden", "Julissa", "Ian",
    # Well-known investors, for clone/benchmark portfolios built from
    # public 13F/ETF disclosures -- see examples/investor_portfolios.csv.
    "Warren Buffett", "Cathie Wood", "Bill Ackman",
]


def _add_column_if_missing(conn, table, column, column_def):
    """ALTER TABLE ... ADD COLUMN is the only way SQLite lets an
    existing table pick up a newly added column -- CREATE TABLE IF NOT
    EXISTS silently does nothing once the table already exists. This
    checks first so it's safe to call unconditionally on every run."""
    existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing_columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")


def init_db(conn):
    conn.executescript(SCHEMA)
    _add_column_if_missing(conn, "quotes", "previous_close", "REAL")
    conn.executescript(SCHEMA_TRIGGERS_AND_VIEWS)
    for owner in PORTFOLIO_OWNERS:
        conn.execute("INSERT OR IGNORE INTO portfolio_owners (name) VALUES (?)", (owner,))
    conn.commit()


# yfinance's `exchange` field returns short internal codes rather than
# full names. This maps the common ones to a human-readable name.
# Codes not found here fall back to just storing the raw code as-is.
EXCHANGE_FULL_NAMES = {
    "NMS": "NASDAQ",
    "NGM": "NASDAQ Global Market",
    "NCM": "NASDAQ Capital Market",
    "NYQ": "New York Stock Exchange",
    "ASE": "NYSE American",
    "PCX": "NYSE Arca",
    "BATS": "Cboe BZX Exchange",
    "LSE": "London Stock Exchange",
    "TOR": "Toronto Stock Exchange",
    "GER": "Deutsche Börse Xetra",
    "PAR": "Euronext Paris",
    "AMS": "Euronext Amsterdam",
    "MIL": "Borsa Italiana",
    "HKG": "Hong Kong Stock Exchange",
    "SHH": "Shanghai Stock Exchange",
    "SHZ": "Shenzhen Stock Exchange",
    "TYO": "Tokyo Stock Exchange",
    "ASX": "Australian Securities Exchange",
    "NSE": "National Stock Exchange of India",
    "BSE": "Bombay Stock Exchange",
}


def get_exchange_full_name(code):
    if not code or code == "N/A":
        return None
    return EXCHANGE_FULL_NAMES.get(code.upper(), code)


def get_or_create(conn, table, name, full_name=None):
    """Look up a row by name in a lookup table (industries/sectors/exchanges),
    inserting it if it doesn't exist yet. Returns the row id, or None
    if name is falsy/N/A. If full_name is provided (exchanges table),
    it's stored/refreshed alongside the short code."""
    if not name or name == "N/A":
        return None
    cur = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        row_id = row[0]
        if full_name is not None:
            conn.execute(f"UPDATE {table} SET exchange_name = ? WHERE id = ?", (full_name, row_id))
            conn.commit()
        return row_id
    if full_name is not None:
        cur = conn.execute(f"INSERT INTO {table} (name, exchange_name) VALUES (?, ?)", (name, full_name))
    else:
        cur = conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def get_or_create_company(conn, symbol, name, industry, sector, exchange):
    industry_id = get_or_create(conn, "industries", industry)
    sector_id = get_or_create(conn, "sectors", sector)
    exchange_id = get_or_create(conn, "exchanges", exchange, full_name=get_exchange_full_name(exchange))

    cur = conn.execute("SELECT id FROM companies WHERE symbol = ?", (symbol,))
    row = cur.fetchone()
    if row:
        company_id = row[0]
        # Keep company info fresh in case name/industry/sector/exchange changed.
        conn.execute(
            "UPDATE companies SET name = ?, industry_id = ?, sector_id = ?, exchange_id = ? WHERE id = ?",
            (name, industry_id, sector_id, exchange_id, company_id),
        )
        conn.commit()
        return company_id

    cur = conn.execute(
        "INSERT INTO companies (symbol, name, industry_id, sector_id, exchange_id) VALUES (?, ?, ?, ?, ?)",
        (symbol, name, industry_id, sector_id, exchange_id),
    )
    conn.commit()
    return cur.lastrowid


# --------------------------------------------------------------------------
# Manual company management: add or edit a company's static info by hand
# (useful before it's ever been fetched, or to correct/override data),
# and delete a company along with every record that references it.
# --------------------------------------------------------------------------

def add_or_edit_company(conn, symbol, name=None, industry=None, sector=None, exchange=None):
    """Create a company if it doesn't exist, or update whichever fields
    were passed (None fields are left untouched on an existing company).
    Returns (company_id, created_bool)."""
    symbol = symbol.upper()
    industry_id = get_or_create(conn, "industries", industry) if industry else None
    sector_id = get_or_create(conn, "sectors", sector) if sector else None
    exchange_id = get_or_create(conn, "exchanges", exchange, full_name=get_exchange_full_name(exchange)) if exchange else None

    row = conn.execute(
        "SELECT id, name, industry_id, sector_id, exchange_id FROM companies WHERE symbol = ?",
        (symbol,),
    ).fetchone()

    if row:
        company_id, cur_name, cur_industry_id, cur_sector_id, cur_exchange_id = row
        conn.execute(
            "UPDATE companies SET name = ?, industry_id = ?, sector_id = ?, exchange_id = ? WHERE id = ?",
            (
                name if name is not None else cur_name,
                industry_id if industry is not None else cur_industry_id,
                sector_id if sector is not None else cur_sector_id,
                exchange_id if exchange is not None else cur_exchange_id,
                company_id,
            ),
        )
        conn.commit()
        return company_id, False

    cur = conn.execute(
        "INSERT INTO companies (symbol, name, industry_id, sector_id, exchange_id) VALUES (?, ?, ?, ?, ?)",
        (symbol, name, industry_id, sector_id, exchange_id),
    )
    conn.commit()
    return cur.lastrowid, True


def delete_company(conn, symbol):
    """Delete a company and every record that references it: quotes,
    group memberships, portfolio transactions, and holdings. Returns
    True if a company was found and deleted, False otherwise. There are
    no SQLite foreign-key cascades set up on this schema, so each
    referencing table is cleared explicitly."""
    symbol = symbol.upper()
    row = conn.execute("SELECT id FROM companies WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        return False
    company_id = row[0]

    conn.execute("DELETE FROM quotes WHERE company_id = ?", (company_id,))
    conn.execute("DELETE FROM group_members WHERE company_id = ?", (company_id,))
    conn.execute("DELETE FROM portfolio_transactions WHERE company_id = ?", (company_id,))
    conn.execute("DELETE FROM holdings WHERE company_id = ?", (company_id,))
    conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    conn.commit()
    return True


def print_company(conn, symbol):
    """Print a company's static info plus its latest quote, with market
    cap shown in compact form (e.g. $3.42T) rather than a raw number."""
    symbol = symbol.upper()
    row = conn.execute(
        """
        SELECT c.id, c.symbol, c.name, i.name, s.name, e.exchange_name, e.name
        FROM companies c
        LEFT JOIN industries i ON i.id = c.industry_id
        LEFT JOIN sectors s ON s.id = c.sector_id
        LEFT JOIN exchanges e ON e.id = c.exchange_id
        WHERE c.symbol = ?
        """,
        (symbol,),
    ).fetchone()
    if not row:
        print(f'Company "{symbol}" not found.')
        return

    company_id, sym, name, industry, sector, exch_full, exch_code = row
    print(f"Symbol:    {sym}")
    print(f"Name:      {name or 'N/A'}")
    print(f"Industry:  {industry or 'N/A'}")
    print(f"Sector:    {sector or 'N/A'}")
    print(f"Exchange:  {exch_full or exch_code or 'N/A'}")

    quote = conn.execute(
        """
        SELECT q.current_price, q.previous_close, q.market_cap, q.pe_ratio, q.dividend_yield,
               q.day_high, q.day_low, q.week52_high, q.week52_low, q.volume,
               r.generated_at
        FROM quotes q
        JOIN reports r ON r.id = q.report_id
        WHERE q.company_id = ?
        ORDER BY q.report_id DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()

    if not quote:
        print("No quotes recorded yet.")
        return

    price, previous_close, mcap, pe, div, day_high, day_low, w52_high, w52_low, volume, generated_at = quote

    day_change_str = "N/A"
    if price is not None and previous_close:
        change = price - previous_close
        pct = change / previous_close * 100
        sign = "+" if change >= 0 else ""
        day_change_str = f"{sign}{change:,.2f} ({sign}{pct:.2f}%)"

    print(f"\nLatest quote (as of {generated_at}):")
    print(f"  Price:          {'N/A' if price is None else f'${price:,.2f}'}")
    print(f"  Day Change:     {day_change_str}")
    print(f"  Market Cap:     {format_market_cap(mcap)}")
    print(f"  P/E Ratio:      {'N/A' if pe is None else f'{pe:.2f}'}")
    print(f"  Dividend Yield: {'N/A' if div is None else f'{div:.2f}%'}")
    print(f"  Day Range:      {'N/A' if day_high is None or day_low is None else f'${day_low:,.2f} - ${day_high:,.2f}'}")
    print(f"  52-Week Range:  {'N/A' if w52_high is None or w52_low is None else f'${w52_low:,.2f} - ${w52_high:,.2f}'}")
    print(f"  Volume:         {'N/A' if volume is None else f'{int(volume):,}'}")


def create_report(conn):
    cur = conn.execute(
        "INSERT INTO reports (generated_at) VALUES (?)",
        (datetime.now().isoformat(sep=" ", timespec="seconds"),),
    )
    conn.commit()
    return cur.lastrowid


QUOTE_FIELDS = [
    "pe_ratio", "dividend_yield", "current_price", "previous_close", "day_high", "day_low",
    "week52_high", "week52_low", "volume", "market_cap",
]


def get_last_quote(conn, company_id):
    """Return the most recent previous quote for a company as a dict of
    QUOTE_FIELDS, or None if it has never had a quote recorded before.
    Used to carry forward values (e.g. price) when a fetch comes back
    empty, so quotes are never null except on a company's very first
    quote (when there's nothing yet to carry forward)."""
    row = conn.execute(
        f"""
        SELECT {", ".join(QUOTE_FIELDS)}
        FROM quotes
        WHERE company_id = ?
        ORDER BY report_id DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    if not row:
        return None
    return dict(zip(QUOTE_FIELDS, row))


def fill_missing_from_last_quote(conn, company_id, data):
    """Mutates and returns data: for any QUOTE_FIELDS entry that is None,
    fill it in with the last known value for that field, if one exists.
    A brand-new company with no prior quote is left as None -- there's
    nothing to carry forward yet."""
    last = get_last_quote(conn, company_id)
    if not last:
        return data
    for field in QUOTE_FIELDS:
        if data.get(field) is None and last.get(field) is not None:
            data[field] = last[field]
    return data


def insert_quote(conn, report_id, company_id, data):
    data = fill_missing_from_last_quote(conn, company_id, data)
    conn.execute(
        """
        INSERT INTO quotes (
            report_id, company_id, pe_ratio, dividend_yield, current_price,
            previous_close, day_high, day_low, week52_high, week52_low, volume,
            market_cap, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            company_id,
            data.get("pe_ratio"),
            data.get("dividend_yield"),
            data.get("current_price"),
            data.get("previous_close"),
            data.get("day_high"),
            data.get("day_low"),
            data.get("week52_high"),
            data.get("week52_low"),
            data.get("volume"),
            data.get("market_cap"),
            data.get("error_message"),
        ),
    )
    conn.commit()


FUNDAMENTALS_FIELDS = [
    "price_to_book", "book_value", "return_on_equity", "debt_to_equity",
    "current_ratio", "quick_ratio", "revenue_growth", "earnings_growth",
    "free_cashflow", "operating_cashflow", "profit_margins", "gross_margins",
    "operating_margins", "return_on_assets", "enterprise_value", "peg_ratio",
    "forward_pe", "trailing_pe",
]


def get_last_fundamentals(conn, company_id):
    """Same idea as get_last_quote, but for the fundamentals table: the
    most recent previous fundamentals row for a company, or None."""
    row = conn.execute(
        f"""
        SELECT {", ".join(FUNDAMENTALS_FIELDS)}
        FROM fundamentals
        WHERE company_id = ?
        ORDER BY report_id DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    if not row:
        return None
    return dict(zip(FUNDAMENTALS_FIELDS, row))


def fill_missing_from_last_fundamentals(conn, company_id, data):
    """Same carry-forward behavior as fill_missing_from_last_quote,
    applied to the fundamentals fields instead."""
    last = get_last_fundamentals(conn, company_id)
    if not last:
        return data
    for field in FUNDAMENTALS_FIELDS:
        if data.get(field) is None and last.get(field) is not None:
            data[field] = last[field]
    return data


def insert_fundamentals(conn, report_id, company_id, data):
    data = fill_missing_from_last_fundamentals(conn, company_id, data)
    conn.execute(
        """
        INSERT INTO fundamentals (
            report_id, company_id, price_to_book, book_value, return_on_equity,
            debt_to_equity, current_ratio, quick_ratio, revenue_growth,
            earnings_growth, free_cashflow, operating_cashflow, profit_margins,
            gross_margins, operating_margins, return_on_assets, enterprise_value,
            peg_ratio, forward_pe, trailing_pe
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id, company_id,
            data.get("price_to_book"), data.get("book_value"), data.get("return_on_equity"),
            data.get("debt_to_equity"), data.get("current_ratio"), data.get("quick_ratio"),
            data.get("revenue_growth"), data.get("earnings_growth"), data.get("free_cashflow"),
            data.get("operating_cashflow"), data.get("profit_margins"), data.get("gross_margins"),
            data.get("operating_margins"), data.get("return_on_assets"), data.get("enterprise_value"),
            data.get("peg_ratio"), data.get("forward_pe"), data.get("trailing_pe"),
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def get_unique_filename():
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(OUTPUT_DIR, f"stock_report_{timestamp}.txt")


class _NullWriter:
    """A drop-in stand-in for a file object that discards everything
    written to it. Used when WRITE_TXT_REPORT is False so the rest of
    the fetch loop doesn't need separate code paths for f.write calls."""
    def write(self, *args, **kwargs):
        pass


@contextlib.contextmanager
def get_report_writer(file_path):
    """Yields a real file handle if WRITE_TXT_REPORT is True, otherwise
    a no-op writer -- so nothing gets written to OUTPUT_DIR when text
    reports are disabled, only the database."""
    if WRITE_TXT_REPORT:
        f = open(file_path, "w")
        try:
            yield f
        finally:
            f.close()
    else:
        yield _NullWriter()


def to_number(value):
    """Coerce a numeric-looking value to float, or None if it's not usable
    (e.g. the string 'N/A'). SQLite columns store NULL for these instead
    of the literal text 'N/A', which keeps the data queryable."""
    if value is None or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_dividend_yield(div_yield):
    """
    Normalize yfinance's dividendYield field into a percentage string.

    yfinance has changed formats across versions/tickers: sometimes it
    returns a fraction (0.0092 for 0.92%), sometimes it returns a number
    already expressed as a percentage (0.92 for 0.92%). There's no fully
    reliable way to distinguish these from the number alone, but a
    reasonable heuristic is:
      - Yields above ~10% are extremely rare for real stocks, so a value
        greater than ~0.10 is most likely already a percentage.
      - Otherwise, treat it as a fraction and multiply by 100.
    This is still a heuristic, not a guarantee -- if you need certainty,
    cross-check against ticker.info.get('trailingAnnualDividendYield').

    Returns a tuple of (display_string, numeric_percentage_or_None).
    """
    if div_yield is None or div_yield == 0:
        return "N/A", None
    if div_yield > 0.10:
        return f"{div_yield:.2f}%", round(div_yield, 4)
    pct = div_yield * 100
    return f"{pct:.2f}%", round(pct, 4)


def format_market_cap(value):
    """Format a raw market cap number into a compact, human-readable
    string (e.g. 3420000000000 -> "$3.42T"), instead of a long run of
    digits. Returns "N/A" for None."""
    if value is None or value == "N/A":
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"

    abs_value = abs(value)
    if abs_value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if abs_value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if abs_value >= 1e6:
        return f"${value / 1e6:.2f}M"
    if abs_value >= 1e3:
        return f"${value / 1e3:.2f}K"
    return f"${value:,.2f}"


def get_tracked_symbols(conn):
    """Return every symbol currently stored in the companies table,
    sorted alphabetically. This is how the script knows which stocks
    to refresh when no symbols/filters are passed on the command line."""
    cur = conn.execute("SELECT symbol FROM companies ORDER BY symbol")
    return [row[0] for row in cur.fetchall()]


# --------------------------------------------------------------------------
# Groups: custom named sets of symbols (Portfolio, Dow 30, S&P 500, or
# any watchlist you create). A company can belong to multiple groups.
# --------------------------------------------------------------------------

def get_or_create_bare_company(conn, symbol):
    """Get a company's id, creating a placeholder row (symbol only, no
    metadata) if it doesn't exist yet. Used when adding symbols to a
    group before they've ever been fetched. Unlike get_or_create_company,
    this never overwrites existing name/industry/sector/exchange data."""
    cur = conn.execute("SELECT id FROM companies WHERE symbol = ?", (symbol,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO companies (symbol) VALUES (?)", (symbol,))
    conn.commit()
    return cur.lastrowid


def get_or_create_group(conn, name):
    cur = conn.execute("SELECT id FROM groups WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO groups (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def add_symbols_to_group(conn, group_name, symbols):
    group_id = get_or_create_group(conn, group_name)
    added = 0
    for symbol in symbols:
        symbol = symbol.upper()
        company_id = get_or_create_bare_company(conn, symbol)
        cur = conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, company_id) VALUES (?, ?)",
            (group_id, company_id),
        )
        added += cur.rowcount
    conn.commit()
    return added


def remove_symbols_from_group(conn, group_name, symbols):
    cur = conn.execute("SELECT id FROM groups WHERE name = ?", (group_name,))
    row = cur.fetchone()
    if not row:
        return 0
    group_id = row[0]
    removed = 0
    for symbol in symbols:
        symbol = symbol.upper()
        cur = conn.execute(
            """
            DELETE FROM group_members
            WHERE group_id = ?
              AND company_id = (SELECT id FROM companies WHERE symbol = ?)
            """,
            (group_id, symbol),
        )
        removed += cur.rowcount
    conn.commit()
    return removed


def list_groups(conn):
    cur = conn.execute(
        """
        SELECT g.name, COUNT(gm.company_id)
        FROM groups g
        LEFT JOIN group_members gm ON gm.group_id = g.id
        GROUP BY g.id
        ORDER BY g.name
        """
    )
    return cur.fetchall()


def get_group_symbols(conn, group_name):
    cur = conn.execute(
        """
        SELECT c.symbol
        FROM group_members gm
        JOIN companies c ON c.id = gm.company_id
        JOIN groups g ON g.id = gm.group_id
        WHERE g.name = ?
        ORDER BY c.symbol
        """,
        (group_name,),
    )
    return [row[0] for row in cur.fetchall()]


# --------------------------------------------------------------------------
# Filters by data already on the companies table: sector, industry,
# exchange. These only cover symbols you've fetched at least once, since
# that's when this metadata gets populated.
# --------------------------------------------------------------------------

def get_group_quote_history(conn, group_name):
    """Return, for every company in a group, all of its quotes ordered
    newest-first, keyed by symbol. Used to show the latest price plus
    the change/percent-change versus the prior quote."""
    rows = conn.execute(
        """
        SELECT c.symbol, c.name, q.report_id, q.current_price, q.market_cap,
               q.pe_ratio, q.dividend_yield, q.volume, r.generated_at
        FROM quotes q
        JOIN companies c ON c.id = q.company_id
        JOIN reports r ON r.id = q.report_id
        JOIN group_members gm ON gm.company_id = c.id
        JOIN groups g ON g.id = gm.group_id
        WHERE g.name = ?
        ORDER BY c.symbol, q.report_id DESC
        """,
        (group_name,),
    ).fetchall()

    history = {}
    for symbol, name, report_id, price, mcap, pe, div, volume, generated_at in rows:
        entry = history.setdefault(symbol, {"name": name, "quotes": []})
        entry["quotes"].append({
            "report_id": report_id,
            "price": price,
            "market_cap": mcap,
            "pe_ratio": pe,
            "dividend_yield": div,
            "volume": volume,
            "generated_at": generated_at,
        })
    return history


def print_group_quotes(conn, group_name):
    """Print the latest quote for every symbol in a group, along with
    the price change and percent gain/loss versus that symbol's prior
    quote."""
    history = get_group_quote_history(conn, group_name)
    if not history:
        symbols = get_group_symbols(conn, group_name)
        if not symbols:
            print(f'Group "{group_name}" not found or has no symbols.')
        else:
            print(f'Group "{group_name}" has symbols but no quotes yet. Fetch it first with --group "{group_name}".')
        return

    print(f'{group_name} -- latest quotes ({len(history)} symbol(s)):\n')
    for symbol in sorted(history):
        quotes = history[symbol]["quotes"]
        name = history[symbol]["name"] or ""
        latest = quotes[0]
        # First older quote that actually has a price, to diff against.
        previous = next((q for q in quotes[1:] if q["price"] is not None), None)

        price = latest["price"]
        price_str = "N/A" if price is None else f"${price:,.2f}"

        change_str, pct_str = "N/A", "N/A"
        if price is not None and previous is not None and previous["price"]:
            change = price - previous["price"]
            pct = change / previous["price"] * 100
            sign = "+" if change >= 0 else ""
            change_str = f"{sign}{change:,.2f}"
            pct_str = f"{sign}{pct:.2f}%"

        print(
            f"{symbol:<6} {name:<28.28} price={price_str:<12} "
            f"change={change_str:<10} pct={pct_str:<9} "
            f"mcap={format_market_cap(latest['market_cap']):<10} "
            f"(as of {latest['generated_at']})"
        )


def get_symbol_quote_history(conn, symbol, start_date=None, end_date=None):
    """Every quote recorded for one symbol, all fields, newest first,
    optionally restricted to a date range (inclusive, compared against
    the date portion of the report's generated_at timestamp)."""
    symbol = symbol.upper()
    query = """
        SELECT r.generated_at, q.current_price, q.previous_close, q.pe_ratio, q.dividend_yield,
               q.day_high, q.day_low, q.week52_high, q.week52_low, q.volume,
               q.market_cap, q.error_message
        FROM quotes q
        JOIN companies c ON c.id = q.company_id
        JOIN reports r ON r.id = q.report_id
        WHERE c.symbol = ?
    """
    params = [symbol]
    if start_date:
        query += " AND date(r.generated_at) >= date(?)"
        params.append(start_date)
    if end_date:
        query += " AND date(r.generated_at) <= date(?)"
        params.append(end_date)
    query += " ORDER BY q.report_id DESC"
    return conn.execute(query, params).fetchall()


# Canonical, selectable quote fields for --show-symbol --fields, in the
# order they're displayed by default. "date" (the report timestamp) is
# always shown as the leading [bracket] regardless of --fields, so it
# isn't itself a selectable field.
QUOTE_FIELD_ORDER = [
    "price", "day_change", "market_cap", "pe_ratio", "dividend_yield",
    "day_range", "week52_range", "volume", "error",
]

QUOTE_FIELD_LABELS = {
    "price": "price", "day_change": "day_chg", "market_cap": "mcap", "pe_ratio": "pe",
    "dividend_yield": "div", "day_range": "day", "week52_range": "52wk",
    "volume": "vol", "error": "ERROR",
}

# Friendly aliases -> canonical field name, so --fields is forgiving
# about common shorthand (pe, mcap, div, vol, etc.).
QUOTE_FIELD_ALIASES = {
    "price": "price", "current_price": "price",
    "day_change": "day_change", "change": "day_change", "day_chg": "day_change", "chg": "day_change",
    "market_cap": "market_cap", "mcap": "market_cap", "marketcap": "market_cap",
    "pe": "pe_ratio", "pe_ratio": "pe_ratio", "p/e": "pe_ratio",
    "dividend_yield": "dividend_yield", "div": "dividend_yield", "yield": "dividend_yield", "dividend": "dividend_yield",
    "day_range": "day_range", "day": "day_range", "day_high": "day_range", "day_low": "day_range",
    "week52_range": "week52_range", "52wk": "week52_range", "52_week_range": "week52_range",
    "week52": "week52_range", "52wk_range": "week52_range",
    "volume": "volume", "vol": "volume",
    "error": "error", "error_message": "error",
}


def normalize_quote_fields(raw_fields):
    """Turn whatever was passed to --fields (possibly several args,
    possibly comma-separated within an arg) into a deduplicated list of
    canonical field names, in the order first requested. Raises
    ValueError listing the unrecognized piece(s) if anything doesn't
    match a known field or alias."""
    result = []
    invalid = []
    for arg in raw_fields:
        for piece in arg.split(","):
            piece = piece.strip().lower()
            if not piece:
                continue
            canonical = QUOTE_FIELD_ALIASES.get(piece)
            if canonical is None:
                invalid.append(piece)
            elif canonical not in result:
                result.append(canonical)
    if invalid:
        raise ValueError(
            f"Unknown field(s): {', '.join(invalid)}. "
            f"Valid fields: {', '.join(QUOTE_FIELD_ORDER)} "
            f"(aliases like price/mcap/pe/div/vol/day/52wk also work)."
        )
    return result


def _quote_row_values(row):
    """Return (generated_at, values_dict) for one quote row, where
    values_dict maps each QUOTE_FIELD_ORDER field to its formatted
    display string. Shared by the console formatter and the PDF export."""
    generated_at, price, previous_close, pe, div, day_high, day_low, w52_high, w52_low, volume, mcap, error_message = row

    day_change_str = "N/A"
    if price is not None and previous_close:
        change = price - previous_close
        pct = change / previous_close * 100
        sign = "+" if change >= 0 else ""
        day_change_str = f"{sign}{change:,.2f} ({sign}{pct:.2f}%)"

    values = {
        "price": "N/A" if price is None else f"${price:,.2f}",
        "day_change": day_change_str,
        "market_cap": format_market_cap(mcap),
        "pe_ratio": "N/A" if pe is None else f"{pe:.2f}",
        "dividend_yield": "N/A" if div is None else f"{div:.2f}%",
        "day_range": "N/A" if day_high is None or day_low is None else f"${day_low:,.2f}-${day_high:,.2f}",
        "week52_range": "N/A" if w52_high is None or w52_low is None else f"${w52_low:,.2f}-${w52_high:,.2f}",
        "volume": "N/A" if volume is None else f"{int(volume):,}",
        "error": error_message or "(none)",
    }
    return generated_at, values


def _format_quote_row(row, fields=None):
    """Format one (generated_at, price, pe, div, day_high, day_low,
    w52_high, w52_low, volume, mcap, error_message) row as a single
    display line. If fields is given (a list of canonical field names),
    only those are shown, in that order; otherwise every field is shown
    (with the error field only appended when there actually is one, to
    keep the common no-error case uncluttered)."""
    generated_at, values = _quote_row_values(row)
    error_message = row[-1]

    if fields:
        parts = [f"{QUOTE_FIELD_LABELS[f]}={values[f]}" for f in fields]
        return f"[{generated_at}] " + "  ".join(parts)

    default_order = ["price", "day_change", "market_cap", "pe_ratio", "dividend_yield", "day_range", "week52_range", "volume"]
    parts = [f"{QUOTE_FIELD_LABELS[f]}={values[f]}" for f in default_order]
    line = f"[{generated_at}] " + "  ".join(parts)
    if error_message:
        line += f"  ERROR={error_message}"
    return line


def print_symbol_quotes(conn, symbol, start_date=None, end_date=None, full_history=False, fields=None):
    """Print a symbol's quote data. By default shows only the single
    most recent quote and every field; pass full_history=True and/or a
    start_date/end_date to see the full (or date-bounded) history
    instead, and/or fields (a list of canonical field names) to narrow
    which fields are displayed."""
    symbol = symbol.upper()
    company = conn.execute("SELECT name FROM companies WHERE symbol = ?", (symbol,)).fetchone()
    if not company:
        print(f'Symbol "{symbol}" not found. Use --add-company to create it, or fetch it once to auto-create it.')
        return
    name = company[0] or "N/A"

    show_history = full_history or start_date or end_date
    rows = get_symbol_quote_history(conn, symbol, start_date, end_date)

    if not rows:
        if start_date or end_date:
            print(f"No quotes found for {symbol} ({name}) in that date range.")
        else:
            print(f"{symbol} ({name}) has no quotes recorded yet.")
        return

    if not show_history:
        print(f"{symbol} ({name}) -- latest quote:\n")
        print(_format_quote_row(rows[0], fields))
        if len(rows) > 1:
            print(f"\n({len(rows) - 1} earlier quote(s) exist -- use --full-history or --start-date/--end-date to see them.)")
        return

    header = f"{symbol} ({name}) -- {len(rows)} quote(s)"
    if start_date or end_date:
        header += f" from {start_date or 'the beginning'} to {end_date or 'now'}"
    header += ", newest first:\n"
    print(header)
    for row in rows:
        print(_format_quote_row(row, fields))


def get_company_fundamentals_history(conn, symbol, start_date=None, end_date=None):
    """Same idea as get_symbol_quote_history, but reading from the
    fundamentals table instead of quotes."""
    symbol = symbol.upper()
    query = f"""
        SELECT r.generated_at, {", ".join(FUNDAMENTALS_FIELDS)}
        FROM fundamentals f
        JOIN companies c ON c.id = f.company_id
        JOIN reports r ON r.id = f.report_id
        WHERE c.symbol = ?
    """
    params = [symbol]
    if start_date:
        query += " AND date(r.generated_at) >= date(?)"
        params.append(start_date)
    if end_date:
        query += " AND date(r.generated_at) <= date(?)"
        params.append(end_date)
    query += " ORDER BY f.report_id DESC"
    return conn.execute(query, params).fetchall()


# Canonical, selectable fundamentals fields for --show-fundamentals
# --fields, in the order they're displayed by default.
FUNDAMENTALS_FIELD_ORDER = list(FUNDAMENTALS_FIELDS)

FUNDAMENTALS_FIELD_LABELS = {
    "price_to_book": "p/b", "book_value": "book_val", "return_on_equity": "roe",
    "debt_to_equity": "debt/eq", "current_ratio": "current_r", "quick_ratio": "quick_r",
    "revenue_growth": "rev_grw", "earnings_growth": "earn_grw",
    "free_cashflow": "fcf", "operating_cashflow": "ocf",
    "profit_margins": "profit_m", "gross_margins": "gross_m",
    "operating_margins": "oper_m", "return_on_assets": "roa",
    "enterprise_value": "ev", "peg_ratio": "peg", "forward_pe": "fwd_pe",
    "trailing_pe": "trail_pe",
}

# Friendly aliases -> canonical fundamentals field name.
FUNDAMENTALS_FIELD_ALIASES = {
    "price_to_book": "price_to_book", "pb": "price_to_book", "p/b": "price_to_book",
    "book_value": "book_value", "bookvalue": "book_value",
    "return_on_equity": "return_on_equity", "roe": "return_on_equity",
    "debt_to_equity": "debt_to_equity", "de": "debt_to_equity", "d/e": "debt_to_equity", "debt_equity": "debt_to_equity",
    "current_ratio": "current_ratio", "current": "current_ratio",
    "quick_ratio": "quick_ratio", "quick": "quick_ratio",
    "revenue_growth": "revenue_growth", "rev_growth": "revenue_growth", "revgrowth": "revenue_growth",
    "earnings_growth": "earnings_growth", "earnings_grw": "earnings_growth", "earningsgrowth": "earnings_growth",
    "free_cashflow": "free_cashflow", "fcf": "free_cashflow", "free_cash_flow": "free_cashflow",
    "operating_cashflow": "operating_cashflow", "ocf": "operating_cashflow", "operating_cash_flow": "operating_cashflow",
    "profit_margins": "profit_margins", "profit_margin": "profit_margins",
    "gross_margins": "gross_margins", "gross_margin": "gross_margins",
    "operating_margins": "operating_margins", "operating_margin": "operating_margins",
    "return_on_assets": "return_on_assets", "roa": "return_on_assets",
    "enterprise_value": "enterprise_value", "ev": "enterprise_value",
    "peg_ratio": "peg_ratio", "peg": "peg_ratio",
    "forward_pe": "forward_pe", "fpe": "forward_pe", "fwd_pe": "forward_pe",
    "trailing_pe": "trailing_pe", "tpe": "trailing_pe", "trail_pe": "trailing_pe",
}


def normalize_fundamentals_fields(raw_fields):
    """Same behavior as normalize_quote_fields, for the fundamentals
    field set instead."""
    result = []
    invalid = []
    for arg in raw_fields:
        for piece in arg.split(","):
            piece = piece.strip().lower()
            if not piece:
                continue
            canonical = FUNDAMENTALS_FIELD_ALIASES.get(piece)
            if canonical is None:
                invalid.append(piece)
            elif canonical not in result:
                result.append(canonical)
    if invalid:
        raise ValueError(
            f"Unknown field(s): {', '.join(invalid)}. "
            f"Valid fields: {', '.join(FUNDAMENTALS_FIELD_ORDER)} "
            f"(aliases like pb/roe/de/fcf/ocf/roa/ev/peg also work)."
        )
    return result


def _fundamentals_row_values(row):
    """Return (generated_at, values_dict) for one fundamentals row,
    formatted the same way as _format_fundamentals_row. Shared by the
    console formatter and the PDF export."""
    generated_at = row[0]
    raw = dict(zip(FUNDAMENTALS_FIELDS, row[1:]))

    def pct(v):
        return "N/A" if v is None else f"{v * 100:.2f}%"

    def num(v):
        return "N/A" if v is None else f"{v:.2f}"

    values = {
        "price_to_book": num(raw["price_to_book"]),
        "book_value": "N/A" if raw["book_value"] is None else f"${raw['book_value']:.2f}",
        "return_on_equity": pct(raw["return_on_equity"]),
        "debt_to_equity": num(raw["debt_to_equity"]),
        "current_ratio": num(raw["current_ratio"]),
        "quick_ratio": num(raw["quick_ratio"]),
        "revenue_growth": pct(raw["revenue_growth"]),
        "earnings_growth": pct(raw["earnings_growth"]),
        "free_cashflow": format_market_cap(raw["free_cashflow"]),
        "operating_cashflow": format_market_cap(raw["operating_cashflow"]),
        "profit_margins": pct(raw["profit_margins"]),
        "gross_margins": pct(raw["gross_margins"]),
        "operating_margins": pct(raw["operating_margins"]),
        "return_on_assets": pct(raw["return_on_assets"]),
        "enterprise_value": format_market_cap(raw["enterprise_value"]),
        "peg_ratio": num(raw["peg_ratio"]),
        "forward_pe": num(raw["forward_pe"]),
        "trailing_pe": num(raw["trailing_pe"]),
    }
    return generated_at, values


def _format_fundamentals_row(row, fields=None):
    """Format one fundamentals row (generated_at + FUNDAMENTALS_FIELDS,
    in that order) as a single display line. Ratios are shown as plain
    numbers; growth/return/margin fields (already fractions from
    yfinance, e.g. 0.15) are shown as percentages; cash-flow and
    enterprise-value fields use the same compact $ formatting as
    market cap."""
    generated_at, values = _fundamentals_row_values(row)

    selected = fields if fields else FUNDAMENTALS_FIELD_ORDER
    parts = [f"{FUNDAMENTALS_FIELD_LABELS[f]}={values[f]}" for f in selected]
    return f"[{generated_at}] " + "  ".join(parts)


def print_company_fundamentals(conn, symbol, start_date=None, end_date=None, full_history=False, fields=None):
    """Print a symbol's fundamentals data (price-to-book, ROE, debt/
    equity, margins, cash flow, growth, etc). By default shows only the
    single most recent row and every field; pass full_history=True
    and/or a start_date/end_date to see the full (or date-bounded)
    history instead, and/or fields to narrow which fields are shown."""
    symbol = symbol.upper()
    company = conn.execute("SELECT name FROM companies WHERE symbol = ?", (symbol,)).fetchone()
    if not company:
        print(f'Symbol "{symbol}" not found. Use --add-company to create it, or fetch it once to auto-create it.')
        return
    name = company[0] or "N/A"

    show_history = full_history or start_date or end_date
    rows = get_company_fundamentals_history(conn, symbol, start_date, end_date)

    if not rows:
        if start_date or end_date:
            print(f"No fundamentals found for {symbol} ({name}) in that date range.")
        else:
            print(f"{symbol} ({name}) has no fundamentals data recorded yet. Run a normal quote fetch first.")
        return

    if not show_history:
        print(f"{symbol} ({name}) -- latest fundamentals:\n")
        print(_format_fundamentals_row(rows[0], fields))
        if len(rows) > 1:
            print(f"\n({len(rows) - 1} earlier snapshot(s) exist -- use --full-history or --start-date/--end-date to see them.)")
        return

    header = f"{symbol} ({name}) -- {len(rows)} fundamentals snapshot(s)"
    if start_date or end_date:
        header += f" from {start_date or 'the beginning'} to {end_date or 'now'}"
    header += ", newest first:\n"
    print(header)
    for row in rows:
        print(_format_fundamentals_row(row, fields))


def get_symbols_by_sector(conn, sector_name):
    cur = conn.execute(
        """
        SELECT c.symbol FROM companies c
        JOIN sectors s ON s.id = c.sector_id
        WHERE s.name LIKE ?
        ORDER BY c.symbol
        """,
        (sector_name,),
    )
    return [row[0] for row in cur.fetchall()]


def get_symbols_by_industry(conn, industry_name):
    cur = conn.execute(
        """
        SELECT c.symbol FROM companies c
        JOIN industries i ON i.id = c.industry_id
        WHERE i.name LIKE ?
        ORDER BY c.symbol
        """,
        (industry_name,),
    )
    return [row[0] for row in cur.fetchall()]


def get_symbols_by_exchange(conn, exchange_name):
    cur = conn.execute(
        """
        SELECT c.symbol FROM companies c
        JOIN exchanges e ON e.id = c.exchange_id
        WHERE e.name LIKE ? OR e.exchange_name LIKE ?
        ORDER BY c.symbol
        """,
        (exchange_name, exchange_name),
    )
    return [row[0] for row in cur.fetchall()]


def get_symbols_by_portfolio_id(conn, portfolio_id):
    """Symbols currently held (non-zero quantity) in a portfolio, for
    the --portfolio fetch filter -- lets you refresh quotes for just
    what one portfolio actually holds right now."""
    cur = conn.execute(
        """
        SELECT c.symbol
        FROM holdings h
        JOIN companies c ON c.id = h.company_id
        WHERE h.portfolio_id = ? AND h.quantity != 0
        ORDER BY c.symbol
        """,
        (portfolio_id,),
    )
    return [row[0] for row in cur.fetchall()]


# --------------------------------------------------------------------------
# Value screening: surface stocks trading below their sector/industry
# average P/E and/or near their own 52-week low, using each company's
# latest quote. This is a relative-valuation screen, not a
# recommendation -- results need further research, since cheap can also
# mean "correctly priced for declining earnings."
# --------------------------------------------------------------------------

def run_value_screen(conn, group_by="sector", scope=None, min_pe=3.0, max_pe=60.0,
                      min_price=5.0, min_market_cap=1_000_000_000, limit=25):
    """Return candidates trading below their sector/industry peer-group
    average P/E, ranked by how far below (then by proximity to their
    own 52-week low). Peer groups with fewer than 3 qualifying members
    are excluded so the "average" isn't just one or two companies.
    group_by is "sector" or "industry"; scope optionally limits results
    to one sector/industry name (the peer average is still computed
    from the full group, not just the scoped rows)."""
    group_table = "industries" if group_by == "industry" else "sectors"
    group_col = "industry_id" if group_by == "industry" else "sector_id"

    query = f"""
        WITH latest AS (
            SELECT q.company_id, c.symbol, c.name, g.name AS group_name,
                   q.pe_ratio, q.dividend_yield, q.current_price,
                   q.week52_high, q.week52_low, q.market_cap
            FROM quotes q
            JOIN companies c ON c.id = q.company_id
            LEFT JOIN {group_table} g ON g.id = c.{group_col}
            WHERE q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id)
              AND q.pe_ratio IS NOT NULL AND q.pe_ratio BETWEEN ? AND ?
              AND q.current_price IS NOT NULL AND q.current_price > ?
              AND q.market_cap IS NOT NULL AND q.market_cap > ?
              AND q.week52_high IS NOT NULL AND q.week52_low IS NOT NULL AND q.week52_high > q.week52_low
        ),
        scored AS (
            SELECT *,
                AVG(pe_ratio) OVER (PARTITION BY group_name) AS peer_avg_pe,
                COUNT(*) OVER (PARTITION BY group_name) AS peer_count,
                (current_price - week52_low) * 100.0 / (week52_high - week52_low) AS pct_of_52wk_range
            FROM latest
            WHERE group_name IS NOT NULL
        )
        SELECT symbol, name, group_name, pe_ratio, peer_avg_pe, peer_count,
               pct_of_52wk_range, dividend_yield, current_price, market_cap
        FROM scored
        WHERE pe_ratio < peer_avg_pe AND peer_count >= 3
    """
    params = [min_pe, max_pe, min_price, min_market_cap]
    if scope:
        query += " AND group_name = ?"
        params.append(scope)
    query += " ORDER BY (pe_ratio - peer_avg_pe) / peer_avg_pe ASC, pct_of_52wk_range ASC LIMIT ?"
    params.append(limit)

    return conn.execute(query, params).fetchall()


def print_value_screen(rows, group_by="sector"):
    if not rows:
        print("No candidates found. Try loosening --min-pe/--max-pe/--min-price/--min-market-cap, or check --scope spelling.")
        return

    label = "Sector" if group_by != "industry" else "Industry"
    print(f"Value screen -- P/E below {label.lower()} average, {len(rows)} result(s):\n")
    print(f"{'Symbol':<7} {'Name':<26} {label:<22} {'P/E':>7} {'PeerAvg':>8} {'%Below':>8} {'52wkPos':>8} {'Div%':>6}")
    for symbol, name, group_name, pe, peer_avg, peer_count, pct_range, div, price, mcap in rows:
        pct_below = (pe - peer_avg) / peer_avg * 100 if peer_avg else 0.0
        div_str = "N/A" if div is None else f"{div:.2f}"
        print(
            f"{symbol:<7} {(name or 'N/A')[:25]:<26} {(group_name or 'N/A')[:21]:<22} "
            f"{pe:>7.2f} {peer_avg:>8.2f} {pct_below:>7.1f}% {pct_range:>7.1f}% {div_str:>6}"
        )
    print("\nRelative valuation only -- a low P/E can mean undervalued, or it can mean the")
    print("market has correctly priced in declining earnings. Research before acting.")


def run_deep_value_screen(conn, max_pb=3.0, min_roe=0.10, max_debt_equity=150.0,
                           max_peg=2.0, min_price=5.0, min_market_cap=1_000_000_000, limit=25):
    """Screen using the fundamentals table (price-to-book, ROE, debt/
    equity, PEG) joined with each company's latest quote (price, market
    cap, sector). Requires fundamentals data to already exist -- run a
    normal quote fetch first, since that's what populates it."""
    query = """
        WITH latest_q AS (
            SELECT q.company_id, q.current_price, q.market_cap, q.pe_ratio
            FROM quotes q
            WHERE q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id)
        ),
        latest_f AS (
            SELECT f.company_id, f.price_to_book, f.return_on_equity, f.debt_to_equity,
                   f.peg_ratio, f.profit_margins, f.revenue_growth
            FROM fundamentals f
            WHERE f.report_id = (SELECT MAX(f2.report_id) FROM fundamentals f2 WHERE f2.company_id = f.company_id)
        )
        SELECT c.symbol, c.name, s.name AS sector_name,
               lq.current_price, lq.market_cap, lq.pe_ratio,
               lf.price_to_book, lf.return_on_equity, lf.debt_to_equity,
               lf.peg_ratio, lf.profit_margins, lf.revenue_growth
        FROM latest_q lq
        JOIN latest_f lf ON lf.company_id = lq.company_id
        JOIN companies c ON c.id = lq.company_id
        LEFT JOIN sectors s ON s.id = c.sector_id
        WHERE lq.current_price IS NOT NULL AND lq.current_price > ?
          AND lq.market_cap IS NOT NULL AND lq.market_cap > ?
          AND lf.price_to_book IS NOT NULL AND lf.price_to_book > 0 AND lf.price_to_book <= ?
          AND lf.return_on_equity IS NOT NULL AND lf.return_on_equity >= ?
          AND (lf.debt_to_equity IS NULL OR lf.debt_to_equity <= ?)
          AND (lf.peg_ratio IS NULL OR (lf.peg_ratio > 0 AND lf.peg_ratio <= ?))
        ORDER BY lf.price_to_book ASC, lf.return_on_equity DESC
        LIMIT ?
    """
    params = [min_price, min_market_cap, max_pb, min_roe, max_debt_equity, max_peg, limit]
    return conn.execute(query, params).fetchall()


def print_deep_value_screen(rows):
    if not rows:
        print("No candidates found. Try loosening --max-pb/--min-roe/--max-debt-equity/--max-peg,")
        print("or make sure fundamentals data has been fetched (run a normal quote fetch first).")
        return

    print(f"Deep value screen -- low P/B, strong ROE, manageable debt, reasonable PEG, {len(rows)} result(s):\n")
    print(f"{'Symbol':<7} {'Name':<24} {'Sector':<18} {'Price':>9} {'P/B':>6} {'ROE':>7} {'D/E':>7} {'PEG':>6}")
    for symbol, name, sector, price, mcap, pe, pb, roe, de, peg, profit_m, rev_g in rows:
        price_str = "N/A" if price is None else f"${price:,.2f}"
        roe_str = "N/A" if roe is None else f"{roe * 100:.1f}%"
        de_str = "N/A" if de is None else f"{de:.1f}"
        peg_str = "N/A" if peg is None else f"{peg:.2f}"
        print(
            f"{symbol:<7} {(name or 'N/A')[:23]:<24} {(sector or 'N/A')[:17]:<18} "
            f"{price_str:>9} {pb:>6.2f} {roe_str:>7} {de_str:>7} {peg_str:>6}"
        )
    print("\nFundamentals-based screen: cheap relative to book value, profitable (ROE), not")
    print("overleveraged (D/E), and not overpaying for growth (PEG). Research before acting.")


# --------------------------------------------------------------------------
# Portfolios: create portfolios, log buy/sell transactions, and view
# current positions. Holdings are derived automatically from
# portfolio_transactions by the SQL triggers -- these functions never
# write to holdings directly.
# --------------------------------------------------------------------------

def get_owner_id(conn, owner_name):
    cur = conn.execute("SELECT id FROM portfolio_owners WHERE name = ?", (owner_name,))
    row = cur.fetchone()
    return row[0] if row else None


def get_or_create_portfolio(conn, portfolio_name, owner_name):
    owner_id = get_owner_id(conn, owner_name)
    if owner_id is None:
        valid = ", ".join(PORTFOLIO_OWNERS)
        raise ValueError(f'Unknown owner "{owner_name}". Valid owners: {valid}')

    cur = conn.execute(
        "SELECT id FROM portfolios WHERE name = ? AND owner_id = ?",
        (portfolio_name, owner_id),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    cur = conn.execute(
        "INSERT INTO portfolios (name, owner_id) VALUES (?, ?)",
        (portfolio_name, owner_id),
    )
    conn.commit()
    return cur.lastrowid


def find_portfolio_id(conn, portfolio_name, owner_name=None):
    """Look up a portfolio by name, optionally disambiguated by owner
    if two different people have a portfolio with the same name."""
    if owner_name:
        owner_id = get_owner_id(conn, owner_name)
        if owner_id is None:
            return None
        cur = conn.execute(
            "SELECT id FROM portfolios WHERE name = ? AND owner_id = ?",
            (portfolio_name, owner_id),
        )
    else:
        cur = conn.execute("SELECT id FROM portfolios WHERE name = ?", (portfolio_name,))
    rows = cur.fetchall()
    if len(rows) > 1:
        raise ValueError(
            f'Multiple portfolios named "{portfolio_name}" exist for different owners. '
            f"Specify --owner to disambiguate."
        )
    return rows[0][0] if rows else None


def delete_portfolio(conn, portfolio_id):
    """Delete a portfolio and cascade-delete every record that
    references it: transactions and holdings. Works whether or not the
    portfolio has any transactions/holdings at all."""
    conn.execute("DELETE FROM portfolio_transactions WHERE portfolio_id = ?", (portfolio_id,))
    conn.execute("DELETE FROM holdings WHERE portfolio_id = ?", (portfolio_id,))
    conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))
    conn.commit()


def record_transaction(conn, portfolio_id, symbol, transaction_type, transaction_date, quantity, price_per_share):
    company_id = get_or_create_bare_company(conn, symbol.upper())
    conn.execute(
        """
        INSERT INTO portfolio_transactions
            (portfolio_id, company_id, transaction_type, transaction_date, quantity, price_per_share)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (portfolio_id, company_id, transaction_type, transaction_date, quantity, price_per_share),
    )
    conn.commit()


def recompute_holdings(conn, portfolio_id, company_id):
    """Rebuild the holdings row for one (portfolio, company) pair from
    scratch by replaying every remaining transaction in insertion order.
    The holdings-sync triggers only fire on INSERT into
    portfolio_transactions, so editing or deleting a transaction has to
    recompute holdings manually rather than relying on the triggers."""
    conn.execute(
        "DELETE FROM holdings WHERE portfolio_id = ? AND company_id = ?",
        (portfolio_id, company_id),
    )
    txns = conn.execute(
        """
        SELECT transaction_type, transaction_date, quantity, price_per_share
        FROM portfolio_transactions
        WHERE portfolio_id = ? AND company_id = ?
        ORDER BY id
        """,
        (portfolio_id, company_id),
    ).fetchall()

    quantity = 0.0
    avg_price = 0.0
    first_purchase_date = None
    for txn_type, txn_date, qty, price in txns:
        if txn_type == "BUY":
            new_quantity = quantity + qty
            if new_quantity != 0:
                avg_price = ((quantity * avg_price) + (qty * price)) / new_quantity
            quantity = new_quantity
            if first_purchase_date is None:
                first_purchase_date = txn_date
        else:  # SELL
            quantity -= qty

    if txns:
        conn.execute(
            """
            INSERT INTO holdings (portfolio_id, company_id, quantity, avg_purchase_price, first_purchase_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (portfolio_id, company_id, quantity, avg_price, first_purchase_date),
        )
    conn.commit()


def list_transactions(conn, portfolio_id):
    return conn.execute(
        """
        SELECT t.id, t.transaction_date, t.transaction_type, c.symbol, t.quantity, t.price_per_share,
               q.current_price AS latest_price
        FROM portfolio_transactions t
        JOIN companies c ON c.id = t.company_id
        LEFT JOIN quotes q ON q.company_id = t.company_id
            AND q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = t.company_id)
        WHERE t.portfolio_id = ?
        ORDER BY t.transaction_date, t.id
        """,
        (portfolio_id,),
    ).fetchall()


def delete_transaction(conn, transaction_id):
    """Delete a transaction by id and recompute holdings for the
    (portfolio, company) it belonged to. Returns True if it existed."""
    row = conn.execute(
        "SELECT portfolio_id, company_id FROM portfolio_transactions WHERE id = ?",
        (transaction_id,),
    ).fetchone()
    if not row:
        return False
    portfolio_id, company_id = row
    conn.execute("DELETE FROM portfolio_transactions WHERE id = ?", (transaction_id,))
    conn.commit()
    recompute_holdings(conn, portfolio_id, company_id)
    return True


def edit_transaction(conn, transaction_id, txn_type=None, quantity=None, price=None, date=None):
    """Update whichever fields were passed on an existing transaction
    (None fields are left unchanged), then recompute holdings for the
    (portfolio, company) it belongs to. Returns True if it existed."""
    row = conn.execute(
        """
        SELECT portfolio_id, company_id, transaction_type, transaction_date, quantity, price_per_share
        FROM portfolio_transactions WHERE id = ?
        """,
        (transaction_id,),
    ).fetchone()
    if not row:
        return False

    portfolio_id, company_id, cur_type, cur_date, cur_qty, cur_price = row
    conn.execute(
        """
        UPDATE portfolio_transactions
        SET transaction_type = ?, transaction_date = ?, quantity = ?, price_per_share = ?
        WHERE id = ?
        """,
        (
            txn_type if txn_type is not None else cur_type,
            date if date is not None else cur_date,
            quantity if quantity is not None else cur_qty,
            price if price is not None else cur_price,
            transaction_id,
        ),
    )
    conn.commit()
    recompute_holdings(conn, portfolio_id, company_id)
    return True


# --------------------------------------------------------------------------
# Bulk import from a CSV file: buy/sell transactions, transaction edits and
# deletes, watchlist/group membership changes, and company add/edit/delete
# -- all in one file, one action per row, driven by an "action" column.
# --------------------------------------------------------------------------

IMPORT_COLUMNS = [
    "action", "portfolio", "owner", "group", "symbol", "quantity", "price",
    "date", "type", "transaction_id", "company_name", "industry", "sector", "exchange",
]

IMPORT_ACTIONS = {
    "BUY", "SELL", "EDIT_TRANSACTION", "DELETE_TRANSACTION", "DELETE_PORTFOLIO",
    "WATCHLIST", "ADD_TO_GROUP", "REMOVE_FROM_GROUP",
    "ADD_COMPANY", "EDIT_COMPANY", "DELETE_COMPANY",
}

IMPORT_TEMPLATE_COMMENT = """\
# update_quotes.py bulk import file.
# One action per row. Leave columns blank if an action doesn't need them.
# Lines starting with # are ignored.
#
# action can be: BUY, SELL, EDIT_TRANSACTION, DELETE_TRANSACTION, DELETE_PORTFOLIO,
#                WATCHLIST (alias for ADD_TO_GROUP), REMOVE_FROM_GROUP,
#                ADD_COMPANY, EDIT_COMPANY, DELETE_COMPANY
#
#   BUY / SELL          needs: portfolio, symbol, quantity, price  (optional: owner, date -- defaults to today)
#   EDIT_TRANSACTION     needs: transaction_id                      (optional: type, quantity, price, date -- only what's set changes)
#   DELETE_TRANSACTION   needs: transaction_id
#   DELETE_PORTFOLIO      needs: portfolio                           (optional: owner, if the name is ambiguous)
#                          -- deletes the portfolio AND all its transactions/holdings, whether or not it has any data
#   WATCHLIST             needs: group, symbol
#   REMOVE_FROM_GROUP    needs: group, symbol
#   ADD_COMPANY           needs: symbol                              (optional: company_name, industry, sector, exchange)
#   EDIT_COMPANY          needs: symbol                              (optional: company_name, industry, sector, exchange -- only what's set changes)
#   DELETE_COMPANY        needs: symbol  (also deletes its quotes, group memberships, holdings, and transactions)
#
# Use --list-transactions "Portfolio Name" first to find transaction_id
# values for EDIT_TRANSACTION / DELETE_TRANSACTION.
"""

IMPORT_EXAMPLE_ROWS = [
    {"action": "BUY", "portfolio": "Growth", "owner": "Jonathan", "symbol": "AAPL", "quantity": "10", "price": "150.25", "date": "2026-01-05"},
    {"action": "SELL", "portfolio": "Growth", "owner": "Jonathan", "symbol": "AAPL", "quantity": "5", "price": "200.00", "date": "2026-03-01"},
    {"action": "EDIT_TRANSACTION", "transaction_id": "12", "type": "SELL"},
    {"action": "DELETE_TRANSACTION", "transaction_id": "13"},
    {"action": "DELETE_PORTFOLIO", "portfolio": "OldTestPortfolio", "owner": "Ian"},
    {"action": "WATCHLIST", "group": "Watchlist", "symbol": "NVDA"},
    {"action": "REMOVE_FROM_GROUP", "group": "Watchlist", "symbol": "AMD"},
    {"action": "ADD_COMPANY", "symbol": "NVDA", "company_name": "NVIDIA Corp", "industry": "Semiconductors", "sector": "Technology", "exchange": "NASDAQ"},
    {"action": "EDIT_COMPANY", "symbol": "NVDA", "company_name": "NVIDIA Corporation"},
    {"action": "DELETE_COMPANY", "symbol": "ZZZZ"},
]


def write_import_template(path):
    """Write a starter CSV file with the header, inline documentation as
    comments, and one example row per supported action."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(IMPORT_TEMPLATE_COMMENT)
        writer = csv.DictWriter(f, fieldnames=IMPORT_COLUMNS)
        writer.writeheader()
        for row in IMPORT_EXAMPLE_ROWS:
            writer.writerow({col: row.get(col, "") for col in IMPORT_COLUMNS})


def _row_get(row, *keys):
    """Case-insensitive lookup across a row dict for any of the given
    column-name aliases. Returns None (not "") for blank/missing cells."""
    lower_row = {(k or "").strip().lower(): v for k, v in row.items()}
    for key in keys:
        val = lower_row.get(key.lower())
        if val is not None and val.strip() != "":
            return val.strip()
    return None


def _process_import_row(conn, action, row):
    """Dispatch one already-validated import row to the matching
    existing function. Raises ValueError/TypeError on bad/missing data;
    the caller is responsible for catching and reporting per-row errors."""

    if action in ("BUY", "SELL"):
        portfolio_name = _row_get(row, "portfolio")
        symbol = _row_get(row, "symbol")
        quantity = _row_get(row, "quantity", "qty")
        price = _row_get(row, "price", "price_per_share")
        owner = _row_get(row, "owner")
        txn_date = _row_get(row, "date") or datetime.now().strftime("%Y-%m-%d")

        if not portfolio_name or not symbol or quantity is None or price is None:
            raise ValueError(f"{action} rows require portfolio, symbol, quantity, and price.")
        quantity = float(quantity)
        price = float(price)

        portfolio_id = find_portfolio_id(conn, portfolio_name, owner)
        if portfolio_id is None:
            if owner:
                portfolio_id = get_or_create_portfolio(conn, portfolio_name, owner)
            else:
                raise ValueError(
                    f'portfolio "{portfolio_name}" not found. Add an "owner" column to auto-create it, '
                    f"or create it first with --create-portfolio."
                )
        record_transaction(conn, portfolio_id, symbol, action, txn_date, quantity, price)

    elif action == "EDIT_TRANSACTION":
        transaction_id = _row_get(row, "transaction_id", "id")
        if not transaction_id:
            raise ValueError("EDIT_TRANSACTION rows require a transaction_id.")
        transaction_id = int(transaction_id)

        txn_type = _row_get(row, "type")
        if txn_type:
            txn_type = txn_type.upper()
        quantity = _row_get(row, "quantity", "qty")
        price = _row_get(row, "price", "price_per_share")
        txn_date = _row_get(row, "date")

        if txn_type is None and quantity is None and price is None and txn_date is None:
            raise ValueError("EDIT_TRANSACTION rows need at least one of type/quantity/price/date to change.")

        ok = edit_transaction(
            conn, transaction_id,
            txn_type=txn_type,
            quantity=float(quantity) if quantity is not None else None,
            price=float(price) if price is not None else None,
            date=txn_date,
        )
        if not ok:
            raise ValueError(f"transaction {transaction_id} not found.")

    elif action == "DELETE_TRANSACTION":
        transaction_id = _row_get(row, "transaction_id", "id")
        if not transaction_id:
            raise ValueError("DELETE_TRANSACTION rows require a transaction_id.")
        if not delete_transaction(conn, int(transaction_id)):
            raise ValueError(f"transaction {transaction_id} not found.")

    elif action == "DELETE_PORTFOLIO":
        portfolio_name = _row_get(row, "portfolio")
        owner = _row_get(row, "owner")
        if not portfolio_name:
            raise ValueError("DELETE_PORTFOLIO rows require a portfolio.")
        portfolio_id = find_portfolio_id(conn, portfolio_name, owner)
        if portfolio_id is None:
            raise ValueError(f'portfolio "{portfolio_name}" not found.')
        delete_portfolio(conn, portfolio_id)

    elif action in ("WATCHLIST", "ADD_TO_GROUP"):
        group = _row_get(row, "group", "watchlist")
        symbol = _row_get(row, "symbol")
        if not group or not symbol:
            raise ValueError(f"{action} rows require group and symbol.")
        add_symbols_to_group(conn, group, [symbol])

    elif action == "REMOVE_FROM_GROUP":
        group = _row_get(row, "group", "watchlist")
        symbol = _row_get(row, "symbol")
        if not group or not symbol:
            raise ValueError("REMOVE_FROM_GROUP rows require group and symbol.")
        remove_symbols_from_group(conn, group, [symbol])

    elif action == "ADD_COMPANY":
        symbol = _row_get(row, "symbol")
        if not symbol:
            raise ValueError("ADD_COMPANY rows require a symbol.")
        add_or_edit_company(
            conn, symbol,
            _row_get(row, "company_name", "name"),
            _row_get(row, "industry"),
            _row_get(row, "sector"),
            _row_get(row, "exchange"),
        )

    elif action == "EDIT_COMPANY":
        symbol = _row_get(row, "symbol")
        if not symbol:
            raise ValueError("EDIT_COMPANY rows require a symbol.")
        exists = conn.execute("SELECT 1 FROM companies WHERE symbol = ?", (symbol.upper(),)).fetchone()
        if not exists:
            raise ValueError(f'company "{symbol.upper()}" not found. Use ADD_COMPANY to create it.')
        add_or_edit_company(
            conn, symbol,
            _row_get(row, "company_name", "name"),
            _row_get(row, "industry"),
            _row_get(row, "sector"),
            _row_get(row, "exchange"),
        )

    elif action == "DELETE_COMPANY":
        symbol = _row_get(row, "symbol")
        if not symbol:
            raise ValueError("DELETE_COMPANY rows require a symbol.")
        if not delete_company(conn, symbol):
            raise ValueError(f'company "{symbol.upper()}" not found.')

    else:
        # Should be unreachable -- caller validates against IMPORT_ACTIONS first.
        raise ValueError(f"unknown action '{action}'.")


def process_import_file(conn, filepath):
    """Read a bulk-import CSV file and apply every row. Each row is
    committed independently (via the underlying functions it calls), so
    one bad row doesn't stop the rest of the file. Returns
    (success_count, list_of_error_strings)."""
    try:
        with open(filepath, newline="", encoding="utf-8-sig") as fh:
            raw_lines = fh.readlines()
    except OSError as e:
        return 0, [f"Could not open file: {e}"]

    # Keep track of each kept line's original line number (for error
    # messages) while dropping blank lines and '#' comment lines.
    kept = [
        (line_num, line) for line_num, line in enumerate(raw_lines, start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not kept:
        return 0, ["File is empty, or contains only blank/comment lines."]

    line_numbers = [ln for ln, _ in kept]
    csv_lines = [line for _, line in kept]
    reader = csv.DictReader(csv_lines)
    if not reader.fieldnames:
        return 0, ["File has no header row."]

    successes = 0
    errors = []

    unknown_columns = [c for c in reader.fieldnames if c and c.strip().lower() not in IMPORT_COLUMNS]
    if unknown_columns:
        errors.append(
            f"Warning: unrecognized column(s) {', '.join(unknown_columns)} will be ignored. "
            f"Expected columns: {', '.join(IMPORT_COLUMNS)}."
        )

    for offset, row in enumerate(reader):
        original_line_num = line_numbers[offset + 1]  # +1 to skip the header line
        action = (_row_get(row, "action") or "").upper()
        if not action:
            errors.append(f"Line {original_line_num}: missing 'action' column.")
            continue
        if action not in IMPORT_ACTIONS:
            errors.append(
                f"Line {original_line_num}: unknown action '{action}'. "
                f"Valid actions: {', '.join(sorted(IMPORT_ACTIONS))}."
            )
            continue
        try:
            _process_import_row(conn, action, row)
            successes += 1
        except Exception as e:
            errors.append(f"Line {original_line_num} ({action}): {e}")

    return successes, errors


# --------------------------------------------------------------------------
# PDF export: --pdf PATH combines with --show-portfolio, --show-symbol,
# --show-fundamentals, or --show-group to additionally write a nicely
# formatted PDF report, on top of the normal console output. reportlab
# is imported lazily here so it's only required when --pdf is actually
# used.
# --------------------------------------------------------------------------

def build_pdf_output_path(path):
    """Given whatever filename/path was passed to --pdf, return the
    actual path the PDF is written to: always under PDF_OUTPUT_DIR
    (creating it if needed), with a YYYYMMDDHHMMSS timestamp appended
    before the extension -- e.g. "growth.pdf" becomes
    "/sdcard/documents/growth_20260726220301.pdf". This means repeated
    exports never overwrite each other, and any directory portion of
    the original --pdf value is ignored in favor of PDF_OUTPUT_DIR."""
    os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)
    base = os.path.basename(path)
    root, ext = os.path.splitext(base)
    if not root:
        root = "report"
    if not ext:
        ext = ".pdf"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return os.path.join(PDF_OUTPUT_DIR, f"{root}_{timestamp}{ext}")


def write_pdf_report(path, title, sections, footer_lines=None):
    """Write a simple multi-section PDF report and return the actual
    path it was written to (see build_pdf_output_path -- this is not
    necessarily the same as the `path` argument). Each section is a
    dict:
        {
            "heading": str,               # section title, e.g. a portfolio or symbol name
            "info_lines": [str, ...],     # optional plain lines under the heading
            "header": [str, ...],         # optional table header row
            "rows": [[str, ...], ...],    # optional table body rows (parallel to header)
            "note_lines": [str, ...],     # optional plain lines after the table
        }
    footer_lines are printed once at the very end of the document (e.g.
    a grand total across multiple portfolios)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    output_path = build_pdf_output_path(path)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PdfTitle", parent=styles["Title"], fontSize=18, spaceAfter=2)
    generated_style = ParagraphStyle("PdfGenerated", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#666666"), spaceAfter=14)
    heading_style = ParagraphStyle("PdfHeading", parent=styles["Heading2"], fontSize=13, textColor=colors.HexColor("#1a3d5c"), spaceAfter=4)
    info_style = ParagraphStyle("PdfInfo", parent=styles["Normal"], fontSize=9.5, spaceAfter=3)
    note_style = ParagraphStyle("PdfNote", parent=styles["Normal"], fontSize=8.5, textColor=colors.HexColor("#777777"), spaceBefore=4)
    footer_style = ParagraphStyle("PdfFooter", parent=styles["Normal"], fontSize=11, fontName="Helvetica-Bold", spaceBefore=10)
    header_cell_style = ParagraphStyle("PdfHeaderCell", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.5, leading=10.5, textColor=colors.white)
    body_cell_style = ParagraphStyle("PdfBodyCell", parent=styles["Normal"], fontSize=8.5, leading=10.5)

    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
    )
    story = [
        Paragraph(title, title_style),
        Paragraph(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", generated_style),
    ]

    avail_width = 7.3 * inch
    for i, section in enumerate(sections):
        if i > 0:
            story.append(Spacer(1, 16))
        if section.get("heading"):
            story.append(Paragraph(section["heading"], heading_style))
        for line in section.get("info_lines", []):
            story.append(Paragraph(line, info_style))

        header = section.get("header")
        rows = section.get("rows")
        if header and rows is not None:
            col_count = len(header)
            col_width = avail_width / col_count
            table_data = [[Paragraph(str(c), header_cell_style) for c in header]]
            for row in rows:
                table_data.append([Paragraph(str(c), body_cell_style) for c in row])
            table = Table(table_data, colWidths=[col_width] * col_count, repeatRows=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3d5c")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(table)

        for line in section.get("note_lines", []):
            story.append(Paragraph(line, note_style))

    if footer_lines:
        story.append(Spacer(1, 10))
        for line in footer_lines:
            story.append(Paragraph(line, footer_style))

    doc.build(story)
    return output_path


def build_portfolio_pdf_section(conn, portfolio_id, heading):
    """Build a write_pdf_report section dict for one portfolio's
    holdings, plus that portfolio's total value (or None). Mirrors what
    print_portfolio shows on the console."""
    positions = get_portfolio_positions(conn, portfolio_id)
    if not positions:
        return {"heading": heading, "info_lines": ["No holdings in this portfolio yet."]}, None

    header = ["Symbol", "Qty", "Avg Cost", "Current", "Day Change", "Gain/Loss", "Value", "As Of"]
    rows = []
    as_of_dates = set()
    for data in positions:
        price = "N/A" if data["current_price"] is None else f"${data['current_price']:.2f}"
        gain = "N/A" if data["pct_gain_loss"] is None else f"{data['pct_gain_loss']:.2f}%"
        value = "N/A" if data["position_value"] is None else f"${data['position_value']:,.2f}"
        day_change = data.get("day_change")
        day_change_pct = data.get("day_change_pct")
        if day_change is None:
            day_str = "N/A"
        else:
            sign = "+" if day_change >= 0 else ""
            day_str = f"{sign}{day_change:.2f} ({sign}{day_change_pct:.2f}%)"
        as_of = data.get("quote_as_of")
        if as_of:
            as_of_dates.add(as_of)
        rows.append([
            data["symbol"], f"{data['quantity']:,.4g}", f"${data['avg_purchase_price']:.2f}",
            price, day_str, gain, value, as_of or "N/A",
        ])

    total_row = conn.execute(
        "SELECT total_portfolio_value FROM portfolio_totals WHERE portfolio_id = ?",
        (portfolio_id,),
    ).fetchone()
    total_value = total_row[0] if total_row and total_row[0] is not None else None

    note_lines = []
    if len(as_of_dates) > 1:
        note_lines.append(
            f"Note: holdings show quotes from {len(as_of_dates)} different fetch times "
            f"({min(as_of_dates)} to {max(as_of_dates)})."
        )
    if total_value is not None:
        note_lines.append(f"Total portfolio value: ${total_value:,.2f}")

    return {"heading": heading, "header": header, "rows": rows, "note_lines": note_lines}, total_value


def build_symbol_quote_pdf_section(conn, symbol, start_date=None, end_date=None, full_history=False, fields=None):
    """Build a write_pdf_report section dict for one symbol's quote
    data -- latest only by default, or full/date-bounded history."""
    symbol = symbol.upper()
    company = conn.execute("SELECT name FROM companies WHERE symbol = ?", (symbol,)).fetchone()
    name = (company[0] if company else None) or "N/A"
    heading = f"{symbol} -- {name}"

    if not company:
        return {"heading": heading, "info_lines": [f'Symbol "{symbol}" not found.']}

    all_rows = get_symbol_quote_history(conn, symbol, start_date, end_date)
    if not all_rows:
        return {"heading": heading, "info_lines": ["No quotes recorded for this symbol/date range."]}

    show_history = full_history or start_date or end_date
    rows_to_show = all_rows if show_history else all_rows[:1]
    selected = fields if fields else QUOTE_FIELD_ORDER

    header = ["Date"] + [QUOTE_FIELD_LABELS[f] for f in selected]
    rows = []
    for row in rows_to_show:
        generated_at, values = _quote_row_values(row)
        rows.append([generated_at] + [values[f] for f in selected])

    info_lines = []
    if not show_history and len(all_rows) > 1:
        info_lines.append(f"Latest quote shown ({len(all_rows) - 1} earlier snapshot(s) not included).")

    return {"heading": heading, "info_lines": info_lines, "header": header, "rows": rows}


def build_fundamentals_pdf_section(conn, symbol, start_date=None, end_date=None, full_history=False, fields=None):
    """Build a write_pdf_report section dict for one symbol's
    fundamentals data -- latest only by default, or full/date-bounded
    history."""
    symbol = symbol.upper()
    company = conn.execute("SELECT name FROM companies WHERE symbol = ?", (symbol,)).fetchone()
    name = (company[0] if company else None) or "N/A"
    heading = f"{symbol} -- {name} (Fundamentals)"

    if not company:
        return {"heading": heading, "info_lines": [f'Symbol "{symbol}" not found.']}

    all_rows = get_company_fundamentals_history(conn, symbol, start_date, end_date)
    if not all_rows:
        return {"heading": heading, "info_lines": ["No fundamentals data recorded for this symbol/date range."]}

    show_history = full_history or start_date or end_date
    rows_to_show = all_rows if show_history else all_rows[:1]
    selected = fields if fields else FUNDAMENTALS_FIELD_ORDER

    header = ["Date"] + [FUNDAMENTALS_FIELD_LABELS[f] for f in selected]
    rows = []
    for row in rows_to_show:
        generated_at, values = _fundamentals_row_values(row)
        rows.append([generated_at] + [values[f] for f in selected])

    info_lines = []
    if not show_history and len(all_rows) > 1:
        info_lines.append(f"Latest snapshot shown ({len(all_rows) - 1} earlier snapshot(s) not included).")

    return {"heading": heading, "info_lines": info_lines, "header": header, "rows": rows}


def build_group_pdf_section(conn, group_name):
    """Build a write_pdf_report section dict for a group's latest
    quotes, price change, and % gain/loss -- mirrors --show-group."""
    history = get_group_quote_history(conn, group_name)
    if not history:
        return {"heading": group_name, "info_lines": ["Group not found, or has no quotes yet."]}

    header = ["Symbol", "Name", "Price", "Change", "% Change", "Market Cap", "As Of"]
    rows = []
    for symbol in sorted(history):
        quotes = history[symbol]["quotes"]
        name = history[symbol]["name"] or "N/A"
        latest = quotes[0]
        previous = next((q for q in quotes[1:] if q["price"] is not None), None)

        price = latest["price"]
        price_str = "N/A" if price is None else f"${price:,.2f}"
        change_str, pct_str = "N/A", "N/A"
        if price is not None and previous is not None and previous["price"]:
            change = price - previous["price"]
            pct = change / previous["price"] * 100
            sign = "+" if change >= 0 else ""
            change_str = f"{sign}{change:,.2f}"
            pct_str = f"{sign}{pct:.2f}%"

        rows.append([symbol, name, price_str, change_str, pct_str, format_market_cap(latest["market_cap"]), latest["generated_at"]])

    return {"heading": f"{group_name} ({len(history)} symbol(s))", "header": header, "rows": rows}


# A condensed default field set for multi-symbol fundamentals tables
# (--show-portfolio/--show-group --fundamentals and their PDF export).
# All 18 fundamentals fields would make an unreadably wide table; these
# six are a reasonable balance of value-relevant metrics. Override with
# --fields to pick different ones.
DEFAULT_MULTI_SYMBOL_FUNDAMENTALS_FIELDS = [
    "price_to_book", "return_on_equity", "debt_to_equity",
    "peg_ratio", "profit_margins", "revenue_growth",
]


def get_group_fundamentals(conn, group_name):
    """Latest fundamentals snapshot for every symbol in a group.
    Returns a list of (symbol, name, fundamentals_row) tuples, where
    fundamentals_row matches get_company_fundamentals_history's shape
    (generated_at + FUNDAMENTALS_FIELDS), or None if that symbol has no
    fundamentals data yet."""
    symbols = get_group_symbols(conn, group_name)
    if not symbols:
        return None
    results = []
    for symbol in sorted(symbols):
        rows = get_company_fundamentals_history(conn, symbol)
        name_row = conn.execute("SELECT name FROM companies WHERE symbol = ?", (symbol,)).fetchone()
        name = (name_row[0] if name_row else None) or "N/A"
        results.append((symbol, name, rows[0] if rows else None))
    return results


def _multi_symbol_fundamentals_table(entries, fields=None):
    """Shared table-building logic for both the console printer and the
    PDF section builder: entries is a list of (symbol, name,
    fundamentals_row_or_None); returns (header, rows)."""
    selected = fields if fields else DEFAULT_MULTI_SYMBOL_FUNDAMENTALS_FIELDS
    header = ["Symbol", "Name"] + [FUNDAMENTALS_FIELD_LABELS[f] for f in selected]
    rows = []
    for symbol, name, fund_row in entries:
        if fund_row is None:
            rows.append([symbol, name] + ["N/A"] * len(selected))
            continue
        _, values = _fundamentals_row_values(fund_row)
        rows.append([symbol, name] + [values[f] for f in selected])
    return header, rows


def print_group_fundamentals(conn, group_name, fields=None):
    entries = get_group_fundamentals(conn, group_name)
    if entries is None:
        print(f'Group "{group_name}" not found or has no symbols.')
        return
    header, rows = _multi_symbol_fundamentals_table(entries, fields)
    print(f"{group_name} -- fundamentals for {len(entries)} symbol(s):\n")
    widths = [8, 26] + [12] * (len(header) - 2)
    print("  ".join(f"{h:<{w}}" for h, w in zip(header, widths)))
    for row in rows:
        print("  ".join(f"{str(c):<{w}}" for c, w in zip(row, widths)))


def build_group_fundamentals_pdf_section(conn, group_name, fields=None):
    entries = get_group_fundamentals(conn, group_name)
    if entries is None:
        return {"heading": group_name, "info_lines": ["Group not found, or has no symbols."]}
    header, rows = _multi_symbol_fundamentals_table(entries, fields)
    return {"heading": f"{group_name} -- Fundamentals ({len(entries)} symbol(s))", "header": header, "rows": rows}


def list_portfolios(conn):
    cur = conn.execute(
        """
        SELECT p.id, p.name, o.name AS owner
        FROM portfolios p
        JOIN portfolio_owners o ON o.id = p.owner_id
        ORDER BY o.name, p.name
        """
    )
    return cur.fetchall()


def get_portfolio_positions(conn, portfolio_id):
    """Return the current holdings of a portfolio as a list of dicts
    (one per symbol), straight from the portfolio_positions view.
    Shared by print_portfolio and the PDF export."""
    rows = conn.execute(
        "SELECT * FROM portfolio_positions WHERE portfolio_id = ? ORDER BY symbol",
        (portfolio_id,),
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM portfolio_positions LIMIT 0").description]
    return [dict(zip(cols, row)) for row in rows]


def get_portfolio_fundamentals(conn, portfolio_id):
    """Latest fundamentals snapshot for every symbol currently held
    (non-zero quantity) in a portfolio. Same (symbol, name,
    fundamentals_row_or_None) shape as get_group_fundamentals."""
    symbols = get_symbols_by_portfolio_id(conn, portfolio_id)
    if not symbols:
        return []
    results = []
    for symbol in sorted(symbols):
        rows = get_company_fundamentals_history(conn, symbol)
        name_row = conn.execute("SELECT name FROM companies WHERE symbol = ?", (symbol,)).fetchone()
        name = (name_row[0] if name_row else None) or "N/A"
        results.append((symbol, name, rows[0] if rows else None))
    return results


def print_portfolio_fundamentals(conn, portfolio_id, fields=None):
    entries = get_portfolio_fundamentals(conn, portfolio_id)
    if not entries:
        print("No holdings in this portfolio yet.")
        return
    header, rows = _multi_symbol_fundamentals_table(entries, fields)
    widths = [8, 26] + [12] * (len(header) - 2)
    print("  ".join(f"{h:<{w}}" for h, w in zip(header, widths)))
    for row in rows:
        print("  ".join(f"{str(c):<{w}}" for c, w in zip(row, widths)))


def build_portfolio_fundamentals_pdf_section(conn, portfolio_id, heading, fields=None):
    entries = get_portfolio_fundamentals(conn, portfolio_id)
    if not entries:
        return {"heading": heading, "info_lines": ["No holdings in this portfolio yet."]}
    header, rows = _multi_symbol_fundamentals_table(entries, fields)
    return {"heading": heading, "header": header, "rows": rows}


def print_portfolio(conn, portfolio_id):
    positions = get_portfolio_positions(conn, portfolio_id)

    if not positions:
        print("No holdings in this portfolio yet.")
        return None

    as_of_dates = set()
    for data in positions:
        price = "N/A" if data["current_price"] is None else f"${data['current_price']:.2f}"
        gain = "N/A" if data["pct_gain_loss"] is None else f"{data['pct_gain_loss']:.2f}%"
        value = "N/A" if data["position_value"] is None else f"${data['position_value']:,.2f}"
        day_change = data.get("day_change")
        day_change_pct = data.get("day_change_pct")
        if day_change is None:
            day_str = "N/A"
        else:
            sign = "+" if day_change >= 0 else ""
            day_str = f"{sign}{day_change:.2f} ({sign}{day_change_pct:.2f}%)"
        as_of = data.get("quote_as_of")
        as_of_str = "  (no quote yet)" if as_of is None else f"  (as of {as_of})"
        if as_of is not None:
            as_of_dates.add(as_of)
        print(
            f"{data['symbol']:<6} qty={data['quantity']:<10.4g} "
            f"avg_cost=${data['avg_purchase_price']:.2f}  current={price}  "
            f"day_chg={day_str}  gain/loss={gain}  value={value}{as_of_str}"
        )

    if len(as_of_dates) > 1:
        print(f"\nNote: holdings show quotes from {len(as_of_dates)} different fetch times "
              f"({min(as_of_dates)} to {max(as_of_dates)}) -- some symbols may be more current than others.")

    total = conn.execute(
        "SELECT total_portfolio_value FROM portfolio_totals WHERE portfolio_id = ?",
        (portfolio_id,),
    ).fetchone()
    if total and total[0] is not None:
        print(f"\nTotal portfolio value: ${total[0]:,.2f}")
        return total[0]
    return None



#
# Dow 30 is a small, fairly stable list, so it's hardcoded here. It can
# still drift when the index committee swaps a component -- if a symbol
# looks wrong, just edit this list and re-run --seed-dow30.
#
# S&P 500 has ~500 constituents that change often enough that hardcoding
# isn't practical, so --seed-sp500 downloads a current list instead. This
# depends on the external source being reachable and up to date -- if it
# fails, add symbols to an "S&P 500" group manually with --add-to-group.
# --------------------------------------------------------------------------

DOW_30_SYMBOLS = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX",
    "DIS", "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"


def seed_dow30(conn):
    added = add_symbols_to_group(conn, "Dow 30", DOW_30_SYMBOLS)
    print(f"Dow 30 group ready: {len(DOW_30_SYMBOLS)} symbols ({added} newly added).")
    print("Note: index composition changes occasionally -- edit DOW_30_SYMBOLS in the script if it drifts.")


def seed_sp500(conn):
    import urllib.request
    import csv
    import io

    print(f"Downloading S&P 500 constituent list from {SP500_CSV_URL} ...")
    try:
        with urllib.request.urlopen(SP500_CSV_URL, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    except Exception as e:
        print(f"Failed to download S&P 500 list: {e}")
        print('You can add symbols manually instead: python3 update_quotes.py --add-to-group "S&P 500" AAPL MSFT ...')
        return

    reader = csv.DictReader(io.StringIO(text))
    symbols = [row["Symbol"].strip().upper() for row in reader if row.get("Symbol")]

    if not symbols:
        print("Downloaded file but found no symbols in it -- the source format may have changed.")
        return

    added = add_symbols_to_group(conn, "S&P 500", symbols)
    print(f"S&P 500 group ready: {len(symbols)} symbols ({added} newly added).")


def fetch_ticker_info(symbol, max_retries=MAX_RETRIES, base_delay=RETRY_BASE_DELAY_SECONDS):
    """
    Fetch a ticker's info from yfinance, retrying with exponential
    backoff on transient errors (e.g. Yahoo's occasional HTTP 500s).
    Raises the last exception if all attempts fail.
    """
    last_exception = None
    for attempt in range(1, max_retries + 1):
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            return ticker, info
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"  {symbol}: attempt {attempt}/{max_retries} failed ({e}); retrying in {delay}s...")
                time.sleep(delay)
    raise last_exception


def fetch_stock_data(symbols):
    if not symbols:
        print("Error: No stock symbols provided.")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    report_id = create_report(conn)

    file_path = get_unique_filename()
    if WRITE_TXT_REPORT:
        print(f"Generating report at: {file_path}")
    print(f"Writing data to database: {DB_PATH}")

    with get_report_writer(file_path) as f:
        f.write(f"Stock Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 40 + "\n\n")

        for i, symbol in enumerate(symbols):
            if i > 0:
                time.sleep(REQUEST_DELAY_SECONDS)

            symbol = symbol.upper()
            try:
                ticker, info = fetch_ticker_info(symbol)

                if not info or (info.get('longName') is None and info.get('regularMarketPrice') is None):
                    f.write(f"Symbol:          {symbol}\n")
                    f.write("Error: No data found for this symbol.\n")
                    f.write("-" * 30 + "\n")

                    company_id = get_or_create_company(conn, symbol, None, None, None, None)
                    insert_quote(conn, report_id, company_id, {"error_message": "No data found for this symbol."})
                    insert_fundamentals(conn, report_id, company_id, {})
                    continue

                name = info.get('longName', 'N/A')
                industry_info = info.get('industry', 'N/A')
                sector_info = info.get('sector', 'N/A')
                exchange_info = info.get('exchange', 'N/A')
                div_yield_str, div_yield_pct = format_dividend_yield(info.get('dividendYield'))
                last_price = info.get('currentPrice', info.get('regularMarketPrice', 'N/A'))
                previous_close = info.get('previousClose', info.get('regularMarketPreviousClose', 'N/A'))
                day_high = info.get('dayHigh', 'N/A')
                day_low = info.get('dayLow', 'N/A')
                week52_high = info.get('fiftyTwoWeekHigh', 'N/A')
                week52_low = info.get('fiftyTwoWeekLow', 'N/A')
                pe_ratio = info.get('trailingPE', 'N/A')
                volume = info.get('regularMarketVolume', info.get('volume', 'N/A'))
                market_cap = info.get('marketCap', 'N/A')

                f.write(f"Company Name:    {name}\n")
                f.write(f"Symbol:          {symbol}\n")
                f.write(f"Industry:        {industry_info}\n")
                f.write(f"Sector:          {sector_info}\n")
                f.write(f"Exchange:        {exchange_info}\n")
                f.write(f"P/E Ratio:       {pe_ratio}\n")
                f.write(f"Dividend Yield:  {div_yield_str}\n")
                f.write(f"Current Price:   {last_price}\n")
                f.write(f"Previous Close:  {previous_close}\n")
                f.write(f"Day's High:      {day_high}\n")
                f.write(f"Day's Low:       {day_low}\n")
                f.write(f"52-Week High:    {week52_high}\n")
                f.write(f"52-Week Low:     {week52_low}\n")
                f.write(f"Volume:          {volume}\n")
                f.write(f"Market Cap:      {format_market_cap(to_number(market_cap))}\n")
                f.write("-" * 30 + "\n")

                company_id = get_or_create_company(
                    conn, symbol,
                    None if name == "N/A" else name,
                    None if industry_info == "N/A" else industry_info,
                    None if sector_info == "N/A" else sector_info,
                    None if exchange_info == "N/A" else exchange_info,
                )
                insert_quote(conn, report_id, company_id, {
                    "pe_ratio": to_number(pe_ratio),
                    "dividend_yield": div_yield_pct,
                    "current_price": to_number(last_price),
                    "previous_close": to_number(previous_close),
                    "day_high": to_number(day_high),
                    "day_low": to_number(day_low),
                    "week52_high": to_number(week52_high),
                    "week52_low": to_number(week52_low),
                    "volume": to_number(volume),
                    "market_cap": to_number(market_cap),
                })
                insert_fundamentals(conn, report_id, company_id, {
                    "price_to_book": to_number(info.get("priceToBook")),
                    "book_value": to_number(info.get("bookValue")),
                    "return_on_equity": to_number(info.get("returnOnEquity")),
                    "debt_to_equity": to_number(info.get("debtToEquity")),
                    "current_ratio": to_number(info.get("currentRatio")),
                    "quick_ratio": to_number(info.get("quickRatio")),
                    "revenue_growth": to_number(info.get("revenueGrowth")),
                    "earnings_growth": to_number(info.get("earningsGrowth")),
                    "free_cashflow": to_number(info.get("freeCashflow")),
                    "operating_cashflow": to_number(info.get("operatingCashflow")),
                    "profit_margins": to_number(info.get("profitMargins")),
                    "gross_margins": to_number(info.get("grossMargins")),
                    "operating_margins": to_number(info.get("operatingMargins")),
                    "return_on_assets": to_number(info.get("returnOnAssets")),
                    "enterprise_value": to_number(info.get("enterpriseValue")),
                    "peg_ratio": to_number(info.get("pegRatio")),
                    "forward_pe": to_number(info.get("forwardPE")),
                    "trailing_pe": to_number(info.get("trailingPE")),
                })

            except Exception as e:
                f.write(f"Symbol:          {symbol}\n")
                f.write(f"Error fetching {symbol}: {e}\n")
                f.write("-" * 30 + "\n")

                company_id = get_or_create_company(conn, symbol, None, None, None, None)
                insert_quote(conn, report_id, company_id, {"error_message": str(e)})
                insert_fundamentals(conn, report_id, company_id, {})

    conn.close()
    print("Report generation complete.")


def build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch/refresh stock quotes, optionally filtered by group, sector, industry, or exchange."
    )
    parser.add_argument("symbols", nargs="*", help="Specific ticker symbols to fetch (overrides all filters below).")

    fetch_filters = parser.add_argument_group("fetch filters (pick at most one)")
    fetch_filters.add_argument("--group", metavar="NAME", help='Fetch symbols in a group, e.g. "Portfolio", "Dow 30", "S&P 500", or any custom group.')
    fetch_filters.add_argument("--sector", metavar="NAME", help='Fetch symbols in a sector, e.g. "Healthcare".')
    fetch_filters.add_argument("--industry", metavar="NAME", help='Fetch symbols in an industry, e.g. "Software - Infrastructure".')
    fetch_filters.add_argument("--exchange", metavar="NAME", help='Fetch symbols on an exchange, e.g. "NASDAQ" or "New York Stock Exchange".')
    fetch_filters.add_argument("--portfolio", nargs="+", metavar="NAME", help="Fetch quotes for every symbol currently held across one or more portfolios (space-separated; combine with --owner if a name is ambiguous).")

    mgmt = parser.add_argument_group("group management (these don't fetch quotes)")
    mgmt.add_argument("--create-group", metavar="NAME", help="Create an empty group.")
    mgmt.add_argument("--add-to-group", nargs="+", metavar=("NAME", "SYMBOL"), help="Add symbols to a group (creates it if needed).")
    mgmt.add_argument("--remove-from-group", nargs="+", metavar=("NAME", "SYMBOL"), help="Remove symbols from a group.")
    mgmt.add_argument("--list-groups", action="store_true", help="List all groups and their symbol counts.")
    mgmt.add_argument("--list-group", metavar="NAME", help="List the symbols in one group.")
    mgmt.add_argument("--show-group", metavar="NAME", help="Show latest quotes for a group, with price change and %% gain/loss since the prior quote.")
    mgmt.add_argument("--seed-dow30", action="store_true", help='Populate a built-in "Dow 30" group.')
    mgmt.add_argument("--seed-sp500", action="store_true", help='Populate an "S&P 500" group by downloading current constituents.')

    port = parser.add_argument_group("portfolio management (these don't fetch quotes)")
    port.add_argument("--create-portfolio", metavar="NAME", help="Create a portfolio (requires --owner).")
    port.add_argument("--owner", metavar="NAME", help=f'Portfolio owner. One of: {", ".join(PORTFOLIO_OWNERS)}.')
    port.add_argument(
        "--buy", nargs=4, metavar=("PORTFOLIO", "SYMBOL", "QUANTITY", "PRICE"),
        help="Log a buy transaction, e.g. --buy Growth AAPL 10 150.25 --date 2026-01-05 (requires --owner the first time a portfolio is created).",
    )
    port.add_argument(
        "--sell", nargs=4, metavar=("PORTFOLIO", "SYMBOL", "QUANTITY", "PRICE"),
        help="Log a sell transaction, e.g. --sell Growth AAPL 5 200.00 --date 2026-03-01",
    )
    port.add_argument("--date", metavar="YYYY-MM-DD", help="Transaction date for --buy/--sell/--edit-transaction (defaults to today for --buy/--sell).")
    port.add_argument("--list-portfolios", action="store_true", help="List all portfolios and their owners.")
    port.add_argument("--show-portfolio", nargs="+", metavar="NAME", help="Show holdings, gain/loss, and total value for one or more portfolios (space-separated names).")
    port.add_argument("--delete-portfolio", metavar="NAME", help="Delete a portfolio and all its transactions/holdings, whether or not it has any data (combine with --owner if ambiguous).")
    port.add_argument("--list-transactions", metavar="PORTFOLIO", help="List transactions (with IDs) for a portfolio, optionally with --owner.")
    port.add_argument("--delete-transaction", type=int, metavar="ID", help="Delete a transaction by ID (from --list-transactions) and recompute holdings.")
    port.add_argument("--edit-transaction", type=int, metavar="ID", help="Edit a transaction by ID; combine with --type/--quantity/--price/--date for the fields to change.")
    port.add_argument("--type", choices=["BUY", "SELL"], help="New transaction type, for --edit-transaction.")
    port.add_argument("--quantity", type=float, metavar="QTY", help="New quantity, for --edit-transaction.")
    port.add_argument("--price", type=float, metavar="PRICE", help="New price per share, for --edit-transaction.")

    company = parser.add_argument_group("company management (these don't fetch quotes)")
    company.add_argument("--add-company", metavar="SYMBOL", help="Add a new company record by hand (combine with --company-name/--industry/--sector/--exchange).")
    company.add_argument("--edit-company", metavar="SYMBOL", help="Edit an existing company's name/industry/sector/exchange (only the fields you pass are changed).")
    company.add_argument("--delete-company", metavar="SYMBOL", help="Delete a company and all its quotes, group memberships, holdings, and transactions.")
    company.add_argument("--show-company", metavar="SYMBOL", help="Show a company's details and latest quote.")
    company.add_argument("--show-symbol", metavar="SYMBOL", help="Show a symbol's quote data (all fields). Defaults to the most recent quote -- combine with --full-history or --start-date/--end-date to see more.")
    company.add_argument("--show-fundamentals", metavar="SYMBOL", help="Show a symbol's fundamentals data (price-to-book, ROE, debt/equity, margins, cash flow, growth, etc). Defaults to the most recent snapshot -- combine with --full-history or --start-date/--end-date to see more.")
    company.add_argument("--full-history", action="store_true", help="With --show-symbol, show every quote ever recorded instead of just the latest.")
    company.add_argument("--start-date", metavar="YYYY-MM-DD", help="With --show-symbol, only show quotes on/after this date.")
    company.add_argument("--end-date", metavar="YYYY-MM-DD", help="With --show-symbol, only show quotes on/before this date.")
    company.add_argument("--fields", nargs="+", metavar="FIELD", help="With --show-symbol, only show these fields (space- or comma-separated). Valid: price, market_cap, pe_ratio, dividend_yield, day_range, week52_range, volume, error (aliases like mcap/pe/div/vol also work).")
    company.add_argument("--company-name", metavar="NAME", help="Company name, for --add-company/--edit-company.")

    bulk = parser.add_argument_group("bulk import from a CSV file (these don't fetch quotes)")
    bulk.add_argument("--import-file", metavar="PATH", help="Bulk-apply buy/sell transactions, transaction edits/deletes, watchlist/group changes, and company add/edit/delete from a CSV file.")
    bulk.add_argument("--import-template", metavar="PATH", help="Write a starter CSV file at PATH with column headers, inline docs, and one example row per supported action.")

    value = parser.add_argument_group("value screening (these don't fetch quotes)")
    value.add_argument("--value-screen", action="store_true", help="Screen for stocks trading below their sector/industry average P/E and show how close each is to its 52-week low.")
    value.add_argument("--group-by", choices=["sector", "industry"], default="sector", help="Peer group to compare P/E against for --value-screen (default: sector).")
    value.add_argument("--scope", metavar="NAME", help="Limit --value-screen results to one sector/industry name (peer averages are still computed from the full peer group, not just the scoped rows).")
    value.add_argument("--min-pe", type=float, default=3.0, metavar="N", help="Minimum P/E to consider for --value-screen (default: 3).")
    value.add_argument("--max-pe", type=float, default=60.0, metavar="N", help="Maximum P/E to consider for --value-screen (default: 60) -- filters out broken/extreme P/E data.")
    value.add_argument("--min-price", type=float, default=5.0, metavar="N", help="Minimum share price to consider for --value-screen (default: 5) -- filters out penny stocks.")
    value.add_argument("--min-market-cap", type=float, default=1_000_000_000, metavar="N", help="Minimum market cap to consider for --value-screen (default: 1000000000).")
    value.add_argument("--limit", type=int, default=25, metavar="N", help="Max results to show for --value-screen (default: 25).")
    value.add_argument("--deep-value-screen", action="store_true", help="Screen using fundamentals data: low price-to-book, strong ROE, manageable debt/equity, reasonable PEG (requires fundamentals -- run a normal quote fetch first to populate it).")
    value.add_argument("--max-pb", type=float, default=3.0, metavar="N", help="Maximum price-to-book for --deep-value-screen (default: 3).")
    value.add_argument("--min-roe", type=float, default=0.10, metavar="N", help="Minimum return on equity (as a fraction, e.g. 0.10 = 10%%) for --deep-value-screen (default: 0.10).")
    value.add_argument("--max-debt-equity", type=float, default=150.0, metavar="N", help="Maximum debt-to-equity for --deep-value-screen (default: 150).")
    value.add_argument("--max-peg", type=float, default=2.0, metavar="N", help="Maximum PEG ratio for --deep-value-screen (default: 2). Companies with no PEG data are not excluded by this filter.")

    pdf_group = parser.add_argument_group("PDF export and fundamentals view")
    pdf_group.add_argument("--pdf", metavar="NAME", help="Also write the result of --show-portfolio, --show-symbol, --show-fundamentals, or --show-group to a PDF file. Always saved under /sdcard/documents/ with a YYYYMMDDHHMMSS timestamp appended to the filename, e.g. --pdf growth.pdf -> /sdcard/documents/growth_20260726220301.pdf.")
    pdf_group.add_argument("--fundamentals", action="store_true", help="With --show-portfolio or --show-group, show fundamentals metrics (P/B, ROE, debt/equity, PEG, margins, growth) for every symbol instead of price/gain-loss. Combine with --fields to pick which metrics, or --pdf to export.")

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # --- Group management commands: handle and exit, no fetching ---
    if args.create_group:
        get_or_create_group(conn, args.create_group)
        print(f'Group "{args.create_group}" created (or already existed).')
        conn.close()
        return

    if args.add_to_group:
        name, symbols = args.add_to_group[0], args.add_to_group[1:]
        if not symbols:
            print("Provide at least one symbol to add, e.g.:")
            print(f'  python3 update_quotes.py --add-to-group "{name}" AAPL MSFT')
            conn.close()
            return
        added = add_symbols_to_group(conn, name, symbols)
        print(f'Added {added} new symbol(s) to "{name}".')
        conn.close()
        return

    if args.remove_from_group:
        name, symbols = args.remove_from_group[0], args.remove_from_group[1:]
        if not symbols:
            print("Provide at least one symbol to remove.")
            conn.close()
            return
        removed = remove_symbols_from_group(conn, name, symbols)
        print(f'Removed {removed} symbol(s) from "{name}".')
        conn.close()
        return

    if args.list_groups:
        groups = list_groups(conn)
        if not groups:
            print("No groups yet. Create one with --create-group or --add-to-group.")
        else:
            for name, count in groups:
                print(f"{name}: {count} symbol(s)")
        conn.close()
        return

    if args.list_group:
        symbols = get_group_symbols(conn, args.list_group)
        if not symbols:
            print(f'Group "{args.list_group}" not found or has no symbols.')
        else:
            print(f'{args.list_group} ({len(symbols)}): {", ".join(symbols)}')
        conn.close()
        return

    if args.show_group:
        if args.fundamentals:
            fields = None
            if args.fields:
                try:
                    fields = normalize_fundamentals_fields(args.fields)
                except ValueError as e:
                    print(e)
                    conn.close()
                    return
            print_group_fundamentals(conn, args.show_group, fields=fields)
            if args.pdf:
                try:
                    section = build_group_fundamentals_pdf_section(conn, args.show_group, fields=fields)
                    saved_path = write_pdf_report(args.pdf, f"Group Fundamentals -- {args.show_group}", [section])
                    print(f'\nPDF written to "{saved_path}".')
                except ImportError:
                    print("\nCould not write PDF: the reportlab package is required (pip install reportlab).")
            conn.close()
            return

        print_group_quotes(conn, args.show_group)
        if args.pdf:
            try:
                section = build_group_pdf_section(conn, args.show_group)
                saved_path = write_pdf_report(args.pdf, f"Group Snapshot -- {args.show_group}", [section])
                print(f'\nPDF written to "{saved_path}".')
            except ImportError:
                print("\nCould not write PDF: the reportlab package is required (pip install reportlab).")
        conn.close()
        return

    if args.seed_dow30:
        seed_dow30(conn)
        conn.close()
        return

    if args.seed_sp500:
        seed_sp500(conn)
        conn.close()
        return

    # --- Company management commands: handle and exit, no fetching ---
    if args.add_company:
        symbol = args.add_company.upper()
        company_id, created = add_or_edit_company(
            conn, symbol, args.company_name, args.industry, args.sector, args.exchange
        )
        print(f'Company "{symbol}" {"created" if created else "already existed and was updated"}.')
        conn.close()
        return

    if args.edit_company:
        symbol = args.edit_company.upper()
        exists = conn.execute("SELECT 1 FROM companies WHERE symbol = ?", (symbol,)).fetchone()
        if not exists:
            print(f'Company "{symbol}" not found. Use --add-company to create it.')
            conn.close()
            return
        add_or_edit_company(conn, symbol, args.company_name, args.industry, args.sector, args.exchange)
        print(f'Company "{symbol}" updated.')
        conn.close()
        return

    if args.delete_company:
        symbol = args.delete_company.upper()
        if delete_company(conn, symbol):
            print(f'Company "{symbol}" and all its related records (quotes, group memberships, holdings, transactions) were deleted.')
        else:
            print(f'Company "{symbol}" not found.')
        conn.close()
        return

    if args.show_company:
        print_company(conn, args.show_company)
        conn.close()
        return

    if args.show_symbol:
        fields = None
        if args.fields:
            try:
                fields = normalize_quote_fields(args.fields)
            except ValueError as e:
                print(e)
                conn.close()
                return
        print_symbol_quotes(conn, args.show_symbol, start_date=args.start_date, end_date=args.end_date, full_history=args.full_history, fields=fields)
        if args.pdf:
            try:
                section = build_symbol_quote_pdf_section(conn, args.show_symbol, start_date=args.start_date, end_date=args.end_date, full_history=args.full_history, fields=fields)
                saved_path = write_pdf_report(args.pdf, f"Quote Data -- {args.show_symbol.upper()}", [section])
                print(f'\nPDF written to "{saved_path}".')
            except ImportError:
                print("\nCould not write PDF: the reportlab package is required (pip install reportlab).")
        conn.close()
        return

    if args.show_fundamentals:
        fields = None
        if args.fields:
            try:
                fields = normalize_fundamentals_fields(args.fields)
            except ValueError as e:
                print(e)
                conn.close()
                return
        print_company_fundamentals(conn, args.show_fundamentals, start_date=args.start_date, end_date=args.end_date, full_history=args.full_history, fields=fields)
        if args.pdf:
            try:
                section = build_fundamentals_pdf_section(conn, args.show_fundamentals, start_date=args.start_date, end_date=args.end_date, full_history=args.full_history, fields=fields)
                saved_path = write_pdf_report(args.pdf, f"Fundamentals -- {args.show_fundamentals.upper()}", [section])
                print(f'\nPDF written to "{saved_path}".')
            except ImportError:
                print("\nCould not write PDF: the reportlab package is required (pip install reportlab).")
        conn.close()
        return

    if args.import_template:
        try:
            write_import_template(args.import_template)
            print(f'Template written to "{args.import_template}". Fill it in, then run:')
            print(f'  python3 update_quotes.py --import-file "{args.import_template}"')
        except OSError as e:
            print(f"Could not write template: {e}")
        conn.close()
        return

    if args.import_file:
        successes, errors = process_import_file(conn, args.import_file)
        print(f"Import complete: {successes} row(s) applied, {len(errors)} issue(s).")
        for err in errors:
            print(f"  - {err}")
        conn.close()
        return

    if args.value_screen:
        rows = run_value_screen(
            conn, group_by=args.group_by, scope=args.scope,
            min_pe=args.min_pe, max_pe=args.max_pe,
            min_price=args.min_price, min_market_cap=args.min_market_cap,
            limit=args.limit,
        )
        print_value_screen(rows, group_by=args.group_by)
        conn.close()
        return

    if args.deep_value_screen:
        rows = run_deep_value_screen(
            conn, max_pb=args.max_pb, min_roe=args.min_roe,
            max_debt_equity=args.max_debt_equity, max_peg=args.max_peg,
            min_price=args.min_price, min_market_cap=args.min_market_cap,
            limit=args.limit,
        )
        print_deep_value_screen(rows)
        conn.close()
        return

    if args.create_portfolio:
        if not args.owner:
            print(f'--create-portfolio requires --owner. Valid owners: {", ".join(PORTFOLIO_OWNERS)}')
            conn.close()
            return
        try:
            get_or_create_portfolio(conn, args.create_portfolio, args.owner)
            print(f'Portfolio "{args.create_portfolio}" ready for {args.owner}.')
        except ValueError as e:
            print(e)
        conn.close()
        return

    if args.buy or args.sell:
        portfolio_name, symbol, qty_str, price_str = args.buy or args.sell
        txn_type = "BUY" if args.buy else "SELL"
        try:
            quantity = float(qty_str)
            price = float(price_str)
        except ValueError:
            print("Quantity and price must be numbers.")
            conn.close()
            return

        txn_date = args.date or datetime.now().strftime("%Y-%m-%d")

        try:
            portfolio_id = find_portfolio_id(conn, portfolio_name, args.owner)
        except ValueError as e:
            print(e)
            conn.close()
            return

        if portfolio_id is None:
            if args.owner:
                portfolio_id = get_or_create_portfolio(conn, portfolio_name, args.owner)
                print(f'Portfolio "{portfolio_name}" created for {args.owner}.')
            else:
                print(f'Portfolio "{portfolio_name}" not found. Create it with --create-portfolio "{portfolio_name}" --owner NAME')
                conn.close()
                return

        record_transaction(conn, portfolio_id, symbol, txn_type, txn_date, quantity, price)
        print(f"{txn_type} logged: {quantity} shares of {symbol.upper()} @ ${price:.2f} on {txn_date}.")
        conn.close()
        return

    if args.list_transactions:
        try:
            portfolio_id = find_portfolio_id(conn, args.list_transactions, args.owner)
        except ValueError as e:
            print(e)
            conn.close()
            return
        if portfolio_id is None:
            print(f'Portfolio "{args.list_transactions}" not found.')
            conn.close()
            return
        rows = list_transactions(conn, portfolio_id)
        if not rows:
            print("No transactions in this portfolio yet.")
        else:
            for txn_id, txn_date, txn_type, symbol, qty, price, latest_price in rows:
                line = f"[{txn_id}] {txn_date}  {txn_type:<4} {symbol:<6} qty={qty:<10.4g} @ ${price:.2f}"
                if latest_price is None:
                    line += "  latest=N/A"
                else:
                    change = latest_price - price
                    sign = "+" if change >= 0 else ""
                    pct = (change / price * 100) if price else None
                    pct_str = f", {sign}{pct:.2f}%" if pct is not None else ""
                    line += f"  latest=${latest_price:.2f} ({sign}{change:.2f}{pct_str})"
                print(line)
        conn.close()
        return

    if args.delete_transaction is not None:
        if delete_transaction(conn, args.delete_transaction):
            print(f"Transaction {args.delete_transaction} deleted; holdings recalculated.")
        else:
            print(f"Transaction {args.delete_transaction} not found.")
        conn.close()
        return

    if args.edit_transaction is not None:
        if args.type is None and args.quantity is None and args.price is None and args.date is None:
            print("Provide at least one of --type/--quantity/--price/--date to change.")
            conn.close()
            return
        if edit_transaction(
            conn, args.edit_transaction,
            txn_type=args.type, quantity=args.quantity, price=args.price, date=args.date,
        ):
            print(f"Transaction {args.edit_transaction} updated; holdings recalculated.")
        else:
            print(f"Transaction {args.edit_transaction} not found.")
        conn.close()
        return

    if args.list_portfolios:
        portfolios = list_portfolios(conn)
        if not portfolios:
            print("No portfolios yet. Create one with --create-portfolio NAME --owner OWNER")
        else:
            for pid, name, owner in portfolios:
                print(f"{name}  (owner: {owner})")
        conn.close()
        return

    if args.show_portfolio:
        if args.fundamentals:
            fields = None
            if args.fields:
                try:
                    fields = normalize_fundamentals_fields(args.fields)
                except ValueError as e:
                    print(e)
                    conn.close()
                    return
            pdf_sections = []
            for index, name in enumerate(args.show_portfolio):
                try:
                    portfolio_id = find_portfolio_id(conn, name, args.owner)
                except ValueError as e:
                    print(e)
                    if args.pdf:
                        pdf_sections.append({"heading": name, "info_lines": [str(e)]})
                    continue
                if portfolio_id is None:
                    print(f'Portfolio "{name}" not found.')
                    if args.pdf:
                        pdf_sections.append({"heading": name, "info_lines": [f'Portfolio "{name}" not found.']})
                    continue
                if index > 0:
                    print()
                if len(args.show_portfolio) > 1:
                    print(f"=== {name} ===")
                print_portfolio_fundamentals(conn, portfolio_id, fields=fields)
                if args.pdf:
                    section = build_portfolio_fundamentals_pdf_section(conn, portfolio_id, name, fields=fields)
                    pdf_sections.append(section)
            if args.pdf:
                try:
                    title = "Portfolio Fundamentals" if len(args.show_portfolio) == 1 else "Portfolio Fundamentals (Multiple Portfolios)"
                    saved_path = write_pdf_report(args.pdf, title, pdf_sections)
                    print(f'\nPDF written to "{saved_path}".')
                except ImportError:
                    print("\nCould not write PDF: the reportlab package is required (pip install reportlab).")
            conn.close()
            return

        grand_total = 0.0
        any_total_found = False
        pdf_sections = []
        for index, name in enumerate(args.show_portfolio):
            try:
                portfolio_id = find_portfolio_id(conn, name, args.owner)
            except ValueError as e:
                print(e)
                if args.pdf:
                    pdf_sections.append({"heading": name, "info_lines": [str(e)]})
                continue
            if portfolio_id is None:
                print(f'Portfolio "{name}" not found.')
                if args.pdf:
                    pdf_sections.append({"heading": name, "info_lines": [f'Portfolio "{name}" not found.']})
                continue
            if index > 0:
                print()
            if len(args.show_portfolio) > 1:
                print(f"=== {name} ===")
            total = print_portfolio(conn, portfolio_id)
            if total is not None:
                grand_total += total
                any_total_found = True
            if args.pdf:
                section, _ = build_portfolio_pdf_section(conn, portfolio_id, name)
                pdf_sections.append(section)
        if len(args.show_portfolio) > 1 and any_total_found:
            print(f"\nGrand total across {len(args.show_portfolio)} portfolios: ${grand_total:,.2f}")
        if args.pdf:
            try:
                footer = None
                if len(args.show_portfolio) > 1 and any_total_found:
                    footer = [f"Grand total across {len(args.show_portfolio)} portfolios: ${grand_total:,.2f}"]
                title = "Portfolio Snapshot" if len(args.show_portfolio) == 1 else "Portfolio Snapshots"
                saved_path = write_pdf_report(args.pdf, title, pdf_sections, footer_lines=footer)
                print(f'\nPDF written to "{saved_path}".')
            except ImportError:
                print("\nCould not write PDF: the reportlab package is required (pip install reportlab).")
        conn.close()
        return

    if args.delete_portfolio:
        try:
            portfolio_id = find_portfolio_id(conn, args.delete_portfolio, args.owner)
        except ValueError as e:
            print(e)
            conn.close()
            return
        if portfolio_id is None:
            print(f'Portfolio "{args.delete_portfolio}" not found.')
            conn.close()
            return
        delete_portfolio(conn, portfolio_id)
        print(f'Portfolio "{args.delete_portfolio}" and all its transactions/holdings were deleted.')
        conn.close()
        return

    # --- Otherwise, resolve which symbols to fetch quotes for ---
    filters_used = [f for f in (args.group, args.sector, args.industry, args.exchange, args.portfolio) if f]
    if len(filters_used) > 1:
        print("Please use only one of --group / --sector / --industry / --exchange / --portfolio at a time.")
        conn.close()
        return

    if args.symbols:
        tickers = args.symbols
    elif args.group:
        tickers = get_group_symbols(conn, args.group)
        if not tickers:
            print(f'Group "{args.group}" not found or has no symbols.')
            conn.close()
            return
    elif args.sector:
        tickers = get_symbols_by_sector(conn, args.sector)
        if not tickers:
            print(f'No tracked companies found in sector "{args.sector}".')
            conn.close()
            return
    elif args.industry:
        tickers = get_symbols_by_industry(conn, args.industry)
        if not tickers:
            print(f'No tracked companies found in industry "{args.industry}".')
            conn.close()
            return
    elif args.exchange:
        tickers = get_symbols_by_exchange(conn, args.exchange)
        if not tickers:
            print(f'No tracked companies found on exchange "{args.exchange}".')
            conn.close()
            return
    elif args.portfolio:
        ticker_set = set()
        for name in args.portfolio:
            try:
                portfolio_id = find_portfolio_id(conn, name, args.owner)
            except ValueError as e:
                print(e)
                conn.close()
                return
            if portfolio_id is None:
                print(f'Portfolio "{name}" not found.')
                conn.close()
                return
            ticker_set.update(get_symbols_by_portfolio_id(conn, portfolio_id))
        tickers = sorted(ticker_set)
        if not tickers:
            print(f'No current holdings found across portfolio(s): {", ".join(args.portfolio)}.')
            conn.close()
            return
    else:
        tickers = get_tracked_symbols(conn)
        if not tickers:
            print(f"No companies found in {DB_PATH} and no symbols/filters were passed.")
            print("Run with symbols the first time, e.g.: python3 update_quotes.py AAPL MSFT GOOGL")
            conn.close()
            return
        print(f"No symbols/filters passed -- refreshing all {len(tickers)} tracked companies.")

    conn.close()
    fetch_stock_data(tickers)


if __name__ == "__main__":
    main()
