"""
Generates documentation for update_quotes.py as a PDF.

HOW TO UPDATE THIS DOC WHEN A NEW FEATURE IS ADDED:
  1. Find the relevant section in SECTIONS below (or add a new one).
  2. Add/edit entries -- each is a (heading, body_text) or
     (heading, [bullet, bullet, ...]) pair.
  3. Re-run: python3 generate_docs.py
  4. A fresh update_quotes_documentation.pdf is written to OUTPUT_DIR.
"""

import os
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem,
    Table, TableStyle, HRFlowable,
)

OUTPUT_DIR = "/sdcard/Download/"
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "update_quotes_documentation.pdf")

# --------------------------------------------------------------------------
# Content. Each top-level entry is a section: (title, [items]).
# Each item is either:
#   ("subheading", "paragraph text")
#   ("subheading", ["bullet 1", "bullet 2", ...])
#   ("code", "literal command / code block text")
# --------------------------------------------------------------------------
SECTIONS = [
    ("Overview", [
        ("What this program does",
         "update_quotes.py pulls live stock data from Yahoo Finance (via the "
         "yfinance library) and stores it in a normalized SQLite database "
         "(stock_reports.db) on the SD card. It supports fetching individual "
         "symbols, refreshing everything you've ever tracked, filtering by "
         "group/sector/industry/exchange, and tracking multiple investment "
         "portfolios with buy/sell transaction history."),
        ("Database location", "code:sqlite3 /sdcard/Download/stock_reports.db"),
    ]),

    ("Database Schema", [
        ("Core stock data tables", [
            "companies -- one row per ticker symbol: name, industry, sector, exchange (all normalized as foreign keys, not repeated text)",
            "industries / sectors / exchanges -- deduplicated lookup tables referenced by companies",
            "reports -- one row per script run, with a timestamp",
            "quotes -- the volatile market data per (report, company): price, P/E, dividend yield, day/52-week high-low, volume, market cap",
            "fundamentals -- a separate time-series table (same one-row-per-(report,company) shape as quotes) for deeper valuation/financial-health metrics: price-to-book, book value, ROE, debt/equity, current/quick ratio, revenue/earnings growth, free/operating cash flow, margins, ROA, enterprise value, PEG, forward P/E, trailing P/E. Populated automatically from the same fetch as quotes -- see \"Fundamentals\" below.",
        ]),
        ("Group tables", [
            "groups -- named sets of symbols (Portfolio, Dow 30, S&P 500, or any custom watchlist)",
            "group_members -- many-to-many link between groups and companies",
        ]),
        ("Portfolio tables", [
            "portfolio_owners -- the allowed owners, from the PORTFOLIO_OWNERS list in the script: Jonathan, Braden, Julissa, Ian by default, plus Warren Buffett, Cathie Wood, and Bill Ackman for the example investor portfolios",
            "portfolios -- named portfolios, each linked to one owner",
            "holdings -- current position per (portfolio, stock): quantity, average cost basis, first purchase date. Kept in sync with portfolio_transactions automatically -- never edited directly. New transactions sync via SQL triggers; editing or deleting a transaction triggers a full recompute in Python instead, since SQLite triggers only fire on INSERT.",
            "portfolio_transactions -- the buy/sell log: date, type, quantity, price per share. Rows can be listed, edited, or deleted (see Portfolios section below).",
            "portfolio_positions (view) -- live current price, % gain/loss, and position value, computed from the latest quotes data",
            "portfolio_totals (view) -- total value per portfolio, summed from portfolio_positions",
        ]),
    ]),

    ("Fetching Quotes", [
        ("Fetch specific symbols", "code:python3 update_quotes.py AAPL MSFT GOOGL"),
        ("Refresh everything you've ever tracked (no arguments)",
         "code:python3 update_quotes.py"),
        ("Filter a fetch instead of listing symbols", [
            "--group \"NAME\" -- every symbol in a group",
            "--sector \"NAME\" -- every tracked symbol in a sector",
            "--industry \"NAME\" -- every tracked symbol in an industry",
            "--exchange \"NAME\" -- every tracked symbol on an exchange",
            "--portfolio \"NAME\" [\"NAME\" ...] -- every symbol currently held (non-zero quantity) across one or more portfolios",
            "Only one of these can be used at a time.",
        ]),
        ("Update quotes for one portfolio's holdings",
         "code:python3 update_quotes.py --portfolio \"Growth\""),
        ("Update quotes across multiple portfolios in one command",
         "code:python3 update_quotes.py --portfolio \"Growth\" \"Income\" \"Retirement\""),
        ("Portfolio filter details",
         "This looks at current holdings (quantity != 0), not the full "
         "transaction history -- so a symbol you've completely sold out "
         "of won't be fetched. With multiple portfolios, the symbol list "
         "is the union across all of them (each fetched once, even if "
         "held in more than one portfolio). If a portfolio name is "
         "ambiguous (same name under two different owners), add --owner "
         "to disambiguate, the same way --show-portfolio does."),
        ("How data is stored",
         "Each run creates one row in `reports` and one row per symbol in "
         "`quotes`. Numeric fields that come back as \"N/A\" are converted "
         "to SQL NULL internally so they stay queryable (SUM, AVG, ORDER BY "
         "all work correctly), but see \"No null quotes\" below for what "
         "actually ends up stored."),
        ("No null quotes (carrying forward the last known value)",
         "If a fetch for an already-tracked company comes back with a "
         "missing field -- a transient Yahoo error, a delisted symbol, a "
         "field Yahoo temporarily omits -- that field is filled in with "
         "the company's last known value instead of being stored as NULL, "
         "so a report always shows the last price/PE/market cap/etc. that "
         "was successfully imported rather than a gap. The one exception "
         "is a company's very first quote ever: there's nothing yet to "
         "carry forward, so a brand-new record can still be NULL."),
        ("Market cap display",
         "Market cap is shown in compact form rather than as a long raw "
         "number, e.g. 4772729454592 displays as $4.77T. Values scale to "
         "T (trillions), B (billions), M (millions), or K (thousands) as "
         "appropriate. This applies to the text report and to "
         "--show-company."),
        ("Dividend yield handling",
         "Yahoo's API inconsistently returns dividend yield as either a "
         "fraction (0.0092) or a pre-multiplied percentage (0.92). The "
         "script treats values above 10% as already being a percentage and "
         "everything else as a fraction to convert. This is a heuristic, "
         "not a guarantee."),
    ]),

    ("Groups", [
        ("Concept",
         "A group is just a named set of symbols. NYSE/NASDAQ, sector, and "
         "industry filters reuse data already collected per-company; Dow "
         "30, S&P 500, Portfolio, and any custom watchlist are true groups "
         "stored in the groups/group_members tables."),
        ("Fetch by exchange, sector, or industry", [
            "python3 update_quotes.py --exchange \"NASDAQ\"",
            "python3 update_quotes.py --exchange \"New York Stock Exchange\"",
            "python3 update_quotes.py --sector \"Healthcare\"",
            "python3 update_quotes.py --industry \"Software - Infrastructure\"",
        ]),
        ("Note on exchange/sector/industry filters",
         "These only work for symbols already fetched at least once -- "
         "that's when Yahoo tells you the sector/industry/exchange. Groups "
         "(below) work even for symbols that have never been fetched."),
        ("Built-in index presets", [
            "python3 update_quotes.py --seed-dow30   (populates a \"Dow 30\" group from a built-in list -- edit DOW_30_SYMBOLS in the script if the index composition changes)",
            "python3 update_quotes.py --seed-sp500   (downloads a current S&P 500 constituent list and populates an \"S&P 500\" group)",
        ]),
        ("Custom groups / watchlists", [
            "python3 update_quotes.py --add-to-group \"Watchlist\" NVDA AMD",
            "python3 update_quotes.py --remove-from-group \"Watchlist\" AMD",
            "python3 update_quotes.py --group \"Watchlist\"   (fetch quotes for everything in it)",
        ]),
        ("Inspect groups", [
            "python3 update_quotes.py --list-groups",
            "python3 update_quotes.py --list-group \"Dow 30\"",
        ]),
        ("View a group's quotes",
         "code:python3 update_quotes.py --show-group \"Dow 30\""),
        ("What --show-group shows",
         "For every symbol in the group: its latest price, the change "
         "and percent gain/loss versus that symbol's previous quote, and "
         "market cap in compact form. This reads whatever quotes are "
         "already in the database -- run a normal fetch first (e.g. "
         "--group \"Dow 30\") if a group hasn't been fetched yet."),
    ]),

    ("Portfolios", [
        ("Concept",
         "Any number of portfolios can exist, each owned by one of the "
         "people in PORTFOLIO_OWNERS: Jonathan, Braden, Julissa, and Ian by "
         "default, plus Warren Buffett, Cathie Wood, and Bill Ackman -- "
         "added as owners for the example investor clone portfolios (see "
         "\"Example Investor Portfolios\" below). Add more names to that "
         "list in the script if you want additional owners. You never edit "
         "quantities or average cost directly -- you log buy/sell "
         "transactions (or edit/delete existing ones), and the holdings "
         "table is kept in sync automatically."),
        ("Create a portfolio", "code:python3 update_quotes.py --create-portfolio \"Growth\" --owner Jonathan"),
        ("Log transactions", [
            "python3 update_quotes.py --buy \"Growth\" AAPL 10 150.25 --date 2026-01-05",
            "python3 update_quotes.py --sell \"Growth\" AAPL 5 200.00 --date 2026-03-01",
            "If --date is omitted, today's date is used.",
        ]),
        ("Cost basis logic",
         "Buys use a weighted average: buying 10 shares at $150 then 10 more "
         "at $170 results in 20 shares with an average cost of $160. Selling "
         "reduces quantity only -- it does not change the average cost of "
         "the shares that remain (standard brokerage behavior)."),
        ("List transactions (to get their IDs)", "code:python3 update_quotes.py --list-transactions \"Growth\""),
        ("Latest price on each transaction",
         "Each transaction line also shows that symbol's latest fetched "
         "price alongside the original transaction price, with the $ "
         "and % change between them -- e.g. "
         "\"@ $150.25  latest=$324.90 (+174.65, +116.24%)\". If the "
         "symbol has no quote data yet, this shows \"latest=N/A\" -- run "
         "a normal quote fetch to populate it."),
        ("Edit a transaction", [
            "python3 update_quotes.py --edit-transaction 5 --quantity 20",
            "python3 update_quotes.py --edit-transaction 5 --price 155.00 --date 2026-01-10",
            "python3 update_quotes.py --edit-transaction 5 --type SELL",
            "Only the fields you pass are changed. Holdings are automatically recalculated from scratch afterward.",
        ]),
        ("Delete a transaction", [
            "python3 update_quotes.py --delete-transaction 5",
            "Holdings are automatically recalculated from the remaining transactions afterward.",
        ]),
        ("View a portfolio", [
            "python3 update_quotes.py --show-portfolio \"Growth\"",
            "python3 update_quotes.py --list-portfolios",
        ]),
        ("View multiple portfolios in one command",
         "code:python3 update_quotes.py --show-portfolio \"Growth\" \"Income\" \"Retirement\""),
        ("Multi-portfolio output",
         "Each portfolio is printed under its own \"=== Name ===\" header. "
         "If one of the names isn't found (or is ambiguous without "
         "--owner), that one is reported and skipped -- the rest still "
         "print. When more than one portfolio is shown, a grand total "
         "across all of them is printed at the end."),
        ("Current price, gain/loss, and total value",
         "These are never stored directly -- they're computed live by the "
         "portfolio_positions and portfolio_totals SQL views, using "
         "whatever the most recent quote data says. Run a normal quote "
         "fetch for a symbol before its portfolio numbers will show "
         "anything other than N/A."),
        ("Quote timestamp on each holding",
         "Each line also shows when that symbol's current price was "
         "last fetched, e.g. \"current=$324.90 ... (as of 2026-07-20 "
         "13:26:57)\". If holdings were fetched at different times -- so "
         "some prices are more current than others -- a note is printed "
         "after the holdings list flagging the range of fetch times."),
        ("Day change on each holding",
         "Each line also shows the current price's change for the day "
         "(vs. the previous close), e.g. \"day_chg=+6.83 (+3.62%)\". This "
         "is the day's move, separate from gain/loss (which is vs. your "
         "average purchase price) -- shown as N/A if a symbol's previous "
         "close hasn't been fetched yet."),
        ("Query directly in SQL", "code:SELECT * FROM portfolio_positions WHERE portfolio_name = 'Growth';\nSELECT * FROM portfolio_totals;"),
        ("Delete a portfolio",
         "Deletes the portfolio itself along with all of its "
         "transactions and holdings -- this works whether the portfolio "
         "already has stock data in it or is completely empty. This "
         "cannot be undone."),
        ("Delete command", "code:python3 update_quotes.py --delete-portfolio \"Growth\""),
    ]),

    ("Companies", [
        ("Concept",
         "Companies are normally created automatically the first time you "
         "fetch a symbol, but you can also add, edit, view, or delete a "
         "company record by hand -- useful for correcting bad data, "
         "pre-registering a symbol before its first fetch, or removing a "
         "symbol you no longer want tracked at all."),
        ("Add a company manually",
         "code:python3 update_quotes.py --add-company NVDA --company-name \"NVIDIA Corp\" --industry \"Semiconductors\" --sector \"Technology\" --exchange \"NASDAQ\""),
        ("Edit a company", [
            "python3 update_quotes.py --edit-company NVDA --company-name \"NVIDIA Corporation\"",
            "Only the fields you pass (--company-name/--industry/--sector/--exchange) are changed; the rest are left as-is.",
        ]),
        ("View a company", "code:python3 update_quotes.py --show-company NVDA"),
        ("View a symbol's quote data",
         "--show-symbol gives you every field of a symbol's quote data "
         "(price, day change, market cap, P/E, dividend yield, day "
         "range, 52-week range, volume). By default it shows only the "
         "single most recent quote."),
        ("Day change",
         "Shown right after price, e.g. \"price=$324.90 day_chg=+6.83 "
         "(+3.62%)\" -- the change from the previous close, computed from "
         "the current_price and previous_close fields fetched from "
         "Yahoo. Also shown in --show-company's latest-quote output. "
         "Shows N/A if the previous close hasn't been fetched yet."),
        ("Latest quote (default)", "code:python3 update_quotes.py --show-symbol NVDA"),
        ("Full history instead of just the latest",
         "code:python3 update_quotes.py --show-symbol NVDA --full-history"),
        ("Filter history by date range",
         "code:python3 update_quotes.py --show-symbol NVDA --start-date 2026-01-01 --end-date 2026-02-01"),
        ("Date range details",
         "--start-date and/or --end-date can be used on their own (without "
         "--full-history) to see every quote in that window; either end "
         "can be left off to mean \"from the beginning\" or \"through now\". "
         "Dates are compared against the date portion of each quote's "
         "timestamp, inclusive on both ends."),
        ("Show only specific fields",
         "By default every field is shown. Add --fields to pick just the "
         "ones you want, in whatever order you list them -- works with "
         "the latest-only view, --full-history, and date-range filtering "
         "alike."),
        ("Fields example", "code:python3 update_quotes.py --show-symbol NVDA --fields price volume"),
        ("Fields with full history", "code:python3 update_quotes.py --show-symbol NVDA --fields price pe_ratio --full-history"),
        ("Valid fields and aliases", [
            "price (or current_price)",
            "day_change (or change, day_chg, chg)",
            "market_cap (or mcap, marketcap)",
            "pe_ratio (or pe, p/e)",
            "dividend_yield (or div, yield, dividend)",
            "day_range (or day, day_high, day_low)",
            "week52_range (or 52wk, week52, 52wk_range, 52_week_range)",
            "volume (or vol)",
            "error (or error_message)",
        ]),
        ("Fields syntax",
         "Space-separated (--fields price volume) and comma-separated "
         "(--fields price,volume) both work, and can be mixed. An "
         "unrecognized field name prints the list of valid fields/"
         "aliases instead of running. The report date/timestamp is "
         "always shown regardless of --fields -- it isn't itself a "
         "selectable field, since every quote line needs it for context."),
        ("Delete a company",
         "Deleting a company removes its row from companies AND cascades "
         "to delete every record that references it: quotes, group "
         "memberships, holdings, and portfolio transactions. This cannot "
         "be undone."),
        ("Delete command", "code:python3 update_quotes.py --delete-company NVDA"),
    ]),

    ("Fundamentals", [
        ("Concept",
         "A second, separate data set from quotes: deeper valuation and "
         "financial-health metrics pulled from the same yfinance fetch, "
         "stored in their own `fundamentals` table (not mixed into "
         "`quotes`). Covers price-to-book, book value, return on equity, "
         "debt/equity, current/quick ratio, revenue/earnings growth, "
         "free/operating cash flow, profit/gross/operating margins, "
         "return on assets, enterprise value, PEG ratio, forward P/E, "
         "and trailing P/E."),
        ("How it's populated",
         "Automatically, as a side effect of any normal quote fetch -- "
         "there's no separate command to run. Every symbol you fetch with "
         "plain `python3 update_quotes.py`, `--group`, `--sector`, "
         "`--portfolio`, or specific symbols also gets a fundamentals row "
         "written for that same report."),
        ("No-null carry-forward",
         "Same behavior as quotes: if a field comes back missing on a "
         "fetch, it's filled in from the company's last known value "
         "instead of stored as NULL, except on a company's very first "
         "fundamentals row, where there's nothing yet to carry forward."),
        ("View a symbol's fundamentals",
         "Mirrors --show-symbol: defaults to the latest snapshot and "
         "every field, with the same --full-history, --start-date/"
         "--end-date, and --fields options."),
        ("Latest snapshot (default)", "code:python3 update_quotes.py --show-fundamentals NVDA"),
        ("Full history", "code:python3 update_quotes.py --show-fundamentals NVDA --full-history"),
        ("Specific fields only", "code:python3 update_quotes.py --show-fundamentals NVDA --fields pb roe de peg"),
        ("Valid fields and aliases", [
            "price_to_book (pb)",
            "book_value",
            "return_on_equity (roe)",
            "debt_to_equity (de, d/e)",
            "current_ratio (current)",
            "quick_ratio (quick)",
            "revenue_growth (rev_growth)",
            "earnings_growth",
            "free_cashflow (fcf)",
            "operating_cashflow (ocf)",
            "profit_margins",
            "gross_margins",
            "operating_margins",
            "return_on_assets (roa)",
            "enterprise_value (ev)",
            "peg_ratio (peg)",
            "forward_pe (fpe)",
            "trailing_pe (tpe)",
        ]),
        ("Display formatting",
         "Return/growth/margin fields are stored as fractions (0.15) and "
         "displayed as percentages (15.00%). Free cash flow, operating "
         "cash flow, and enterprise value use the same compact $ "
         "formatting as market cap (e.g. $3.42T). Ratios (P/B, D/E, "
         "current/quick ratio, PEG, forward/trailing P/E) are shown as "
         "plain numbers."),
    ]),

    ("PDF Export", [
        ("Concept",
         "--pdf NAME combines with --show-portfolio, --show-symbol, "
         "--show-fundamentals, or --show-group to additionally write a "
         "nicely formatted PDF report -- on top of, not instead of, the "
         "normal console output. Requires the reportlab package (pip "
         "install reportlab); if it's missing, the console output still "
         "works and a clear message explains how to fix the PDF part."),
        ("Portfolio snapshot to PDF", "code:python3 update_quotes.py --show-portfolio \"Growth\" --pdf growth_snapshot.pdf"),
        ("Multiple portfolios in one PDF",
         "code:python3 update_quotes.py --show-portfolio \"Growth\" \"Income\" --pdf portfolios.pdf"),
        ("What the portfolio PDF contains",
         "One table per portfolio (symbol, quantity, average cost, "
         "current price, day change, gain/loss, value, and the quote's "
         "as-of timestamp), each portfolio's total value, and -- when "
         "more than one portfolio is requested -- a grand total at the "
         "end of the document, matching what's printed to the console."),
        ("Individual stock quote to PDF", "code:python3 update_quotes.py --show-symbol NVDA --pdf nvda_quote.pdf"),
        ("Full history or specific fields also work in the PDF",
         "code:python3 update_quotes.py --show-symbol NVDA --full-history --fields price pe_ratio --pdf nvda_history.pdf"),
        ("Fundamentals to PDF", "code:python3 update_quotes.py --show-fundamentals NVDA --pdf nvda_fundamentals.pdf"),
        ("Group of stock quotes to PDF", "code:python3 update_quotes.py --show-group \"Dow 30\" --pdf dow30_snapshot.pdf"),

        ("Metrics (fundamentals) for a portfolio or group",
         "Add --fundamentals to --show-portfolio or --show-group to show "
         "P/B, ROE, debt/equity, PEG, profit margin, and revenue growth "
         "for every symbol instead of price/gain-loss. Combine with "
         "--fields to pick different metrics, and --pdf to export."),
        ("Portfolio metrics to PDF", "code:python3 update_quotes.py --show-portfolio \"Growth\" --fundamentals --pdf growth_metrics.pdf"),
        ("Multiple portfolios' metrics in one PDF",
         "code:python3 update_quotes.py --show-portfolio \"Growth\" \"Income\" --fundamentals --pdf portfolio_metrics.pdf"),
        ("Group metrics to PDF", "code:python3 update_quotes.py --show-group \"Dow 30\" --fundamentals --pdf dow30_metrics.pdf"),
        ("Choosing different metrics", "code:python3 update_quotes.py --show-portfolio \"Growth\" --fundamentals --fields pb roe fcf ocf --pdf growth_metrics.pdf"),
        ("Default metrics shown",
         "price_to_book, return_on_equity, debt_to_equity, peg_ratio, "
         "profit_margins, revenue_growth -- a deliberately condensed set "
         "so the table stays readable; all 18 fundamentals fields would "
         "make an unreadably wide table. Override with --fields to pick "
         "any of the fields listed in the Fundamentals section above."),
        ("Where PDFs are saved and how they're named",
         "Every PDF is saved under PDF_OUTPUT_DIR (/sdcard/documents/ by "
         "default -- change the constant near the top of the script to "
         "use a different folder), created automatically if it doesn't "
         "exist. Whatever name is passed to --pdf, only its filename is "
         "used -- any directory portion is ignored -- and a "
         "YYYYMMDDHHMMSS timestamp is appended before the extension. For "
         "example, --pdf growth.pdf is actually written to something "
         "like /sdcard/documents/growth_20260726220301.pdf. This means "
         "repeated exports never overwrite each other, and the console "
         "message after --pdf always prints the real, final path."),
    ]),

    ("Value Screening", [
        ("Concept",
         "--value-screen surfaces stocks trading below their sector or "
         "industry peer-group average P/E, and shows how close each one "
         "is to its own 52-week low. This is a relative-valuation screen "
         "for finding research candidates -- not a buy signal. A low P/E "
         "and a price near its 52-week low can mean genuinely "
         "undervalued, or it can mean the market has correctly priced in "
         "declining earnings."),
        ("Run it", "code:python3 update_quotes.py --value-screen"),
        ("What it does",
         "Uses each company's latest quote only. Peer groups (sector or "
         "industry) with fewer than 3 qualifying members are excluded, "
         "so the \"average\" isn't just one or two companies. Results are "
         "ranked by how far below the peer average P/E, then by "
         "proximity to the 52-week low."),
        ("Group by industry instead of sector",
         "code:python3 update_quotes.py --value-screen --group-by industry"),
        ("Limit to one sector or industry",
         "code:python3 update_quotes.py --value-screen --scope \"Healthcare\""),
        ("Scope details",
         "--scope filters which rows are displayed, but the peer-group "
         "average P/E is still computed from every company in that "
         "sector/industry (not just the ones that happen to pass the "
         "other filters) -- so the comparison stays fair."),
        ("Adjustable thresholds", [
            "--min-pe N (default 3) and --max-pe N (default 60) -- excludes broken/extreme P/E data (some symbols have P/E stored as literal infinity from near-zero earnings)",
            "--min-price N (default 5) -- excludes penny stocks",
            "--min-market-cap N (default 1000000000) -- excludes micro-caps and illiquid names",
            "--limit N (default 25) -- how many results to show",
        ]),
        ("Loosen the filters", "code:python3 update_quotes.py --value-screen --min-pe 1 --max-pe 100 --min-price 1 --min-market-cap 100000000 --limit 50"),
        ("No results",
         "If nothing qualifies, the filters were likely too strict (or "
         "--scope was misspelled) -- try loosening --min-pe/--max-pe/"
         "--min-price/--min-market-cap."),
        ("Deep value screen (fundamentals-based)",
         "--deep-value-screen is a second, independent screen that uses "
         "the fundamentals table instead of P/E-vs-peers: low "
         "price-to-book, strong return on equity, manageable debt/equity, "
         "and a reasonable PEG ratio (growth at a reasonable price). "
         "Requires fundamentals data to already exist for the symbols in "
         "question -- run a normal quote fetch first, since that's what "
         "populates it."),
        ("Run it", "code:python3 update_quotes.py --deep-value-screen"),
        ("Adjustable thresholds", [
            "--max-pb N (default 3) -- maximum price-to-book",
            "--min-roe N (default 0.10, i.e. 10%) -- minimum return on equity, as a fraction",
            "--max-debt-equity N (default 150) -- maximum debt-to-equity",
            "--max-peg N (default 2) -- maximum PEG ratio; companies with no PEG data aren't excluded by this filter",
            "--min-price N / --min-market-cap N / --limit N -- same meaning and defaults as --value-screen",
        ]),
        ("Combine the two screens",
         "--value-screen and --deep-value-screen use different data "
         "(quotes-derived peer P/E vs. fundamentals-derived quality "
         "metrics) and different logic, so a symbol worth researching "
         "further is one that shows up on both."),
    ]),

    ("Bulk Import from a File", [
        ("Concept",
         "Instead of running one command at a time, a CSV file can hold "
         "many actions at once -- buy/sell transactions, transaction "
         "edits or deletes, watchlist/group additions or removals, and "
         "company add/edit/delete -- mixed together in any order. Each "
         "row is applied independently, so one bad row doesn't stop the "
         "rest of the file."),
        ("Generate a starter file",
         "code:python3 update_quotes.py --import-template my_import.csv"),
        ("What that produces",
         "A CSV with the full column header, commented-out documentation "
         "of every action and which columns it needs, and one filled-in "
         "example row per action. Edit it, delete the rows you don't "
         "need, and add your own."),
        ("Run the import", "code:python3 update_quotes.py --import-file my_import.csv"),
        ("Columns", [
            "action, portfolio, owner, group, symbol, quantity, price, date, type, transaction_id, company_name, industry, sector, exchange",
            "A row only needs the columns its action uses -- leave the rest blank. Column names are case-insensitive.",
        ]),
        ("Supported actions", [
            "BUY / SELL -- needs portfolio, symbol, quantity, price (optional: owner, date -- defaults to today; if the portfolio doesn't exist yet, include owner and it's created automatically)",
            "EDIT_TRANSACTION -- needs transaction_id (optional: type, quantity, price, date -- only the columns you fill in are changed)",
            "DELETE_TRANSACTION -- needs transaction_id",
            "DELETE_PORTFOLIO -- needs portfolio (optional: owner, if the name is ambiguous) -- deletes the portfolio and all its transactions/holdings, whether or not it has any data",
            "WATCHLIST (alias ADD_TO_GROUP) -- needs group, symbol -- adds a symbol to a group/watchlist",
            "REMOVE_FROM_GROUP -- needs group, symbol",
            "ADD_COMPANY -- needs symbol (optional: company_name, industry, sector, exchange)",
            "EDIT_COMPANY -- needs symbol (optional: company_name, industry, sector, exchange -- only what's filled in changes)",
            "DELETE_COMPANY -- needs symbol; cascades to delete its quotes, group memberships, holdings, and transactions",
        ]),
        ("Finding transaction IDs",
         "Run --list-transactions \"Portfolio Name\" first (or query "
         "portfolio_transactions directly -- see the SQL Reference below) "
         "to get the IDs to put in a transaction_id column."),

        ("Example files provided",
         "Five ready-to-run example CSVs are included alongside this "
         "documentation: one per action category, plus a combined file "
         "showing every action mixed together. Copy whichever fits, edit "
         "the values, and run it with --import-file."),

        ("buy_sell_transactions.csv",
         "code:action,portfolio,owner,group,symbol,quantity,price,date,type,transaction_id,company_name,industry,sector,exchange\nBUY,Growth,Jonathan,,AAPL,10,150.25,2026-01-05,,,,,,\nBUY,Growth,Jonathan,,MSFT,5,410.00,2026-01-10,,,,,,\nSELL,Growth,Jonathan,,AAPL,4,205.75,2026-03-01,,,,,,\nBUY,Income,Braden,,JNJ,15,155.20,2026-02-14,,,,,,\nBUY,Income,Braden,,KO,25,68.40,,,,,,,"),

        ("edit_delete_transactions.csv",
         "code:action,portfolio,owner,group,symbol,quantity,price,date,type,transaction_id,company_name,industry,sector,exchange\nEDIT_TRANSACTION,,,,,20,,,,5,,,,\nEDIT_TRANSACTION,,,,,,70.00,2026-01-15,,8,,,,\nDELETE_TRANSACTION,,,,,,,,,13,,,,"),

        ("watchlist_groups.csv",
         "code:action,portfolio,owner,group,symbol,quantity,price,date,type,transaction_id,company_name,industry,sector,exchange\nWATCHLIST,,,Watchlist,NVDA,,,,,,,,,\nWATCHLIST,,,Watchlist,AMD,,,,,,,,,\nWATCHLIST,,,Watchlist,PLTR,,,,,,,,,\nADD_TO_GROUP,,,Dow 30,CRM,,,,,,,,,\nREMOVE_FROM_GROUP,,,Watchlist,AMD,,,,,,,,,"),

        ("companies.csv",
         "code:action,portfolio,owner,group,symbol,quantity,price,date,type,transaction_id,company_name,industry,sector,exchange\nADD_COMPANY,,,,NVDA,,,,,,NVIDIA Corp,Semiconductors,Technology,NASDAQ\nADD_COMPANY,,,,BRK.B,,,,,,Berkshire Hathaway,Insurance,Financial Services,NYSE\nEDIT_COMPANY,,,,NVDA,,,,,,NVIDIA Corporation,,,\nDELETE_COMPANY,,,,ZZZZ,,,,,,,,,"),

        ("combined_all_actions.csv",
         "code:action,portfolio,owner,group,symbol,quantity,price,date,type,transaction_id,company_name,industry,sector,exchange\nBUY,Growth,Jonathan,,AAPL,10,150.25,2026-01-05,,,,,,\nSELL,Growth,Jonathan,,AAPL,5,200.00,2026-03-01,,,,,,\nEDIT_TRANSACTION,,,,,20,,,,12,,,,\nDELETE_TRANSACTION,,,,,,,,,13,,,,\nWATCHLIST,,,Watchlist,NVDA,,,,,,,,,\nREMOVE_FROM_GROUP,,,Watchlist,AMD,,,,,,,,,\nADD_COMPANY,,,,NVDA,,,,,,NVIDIA Corp,Semiconductors,Technology,NASDAQ\nEDIT_COMPANY,,,,NVDA,,,,,,NVIDIA Corporation,,,\nDELETE_COMPANY,,,,ZZZZ,,,,,,,,,"),

        ("Result reporting",
         "After running, the script prints how many rows succeeded and "
         "lists every row that failed with its line number and reason "
         "(missing data, bad number, unknown action, transaction/company "
         "not found, etc.), so partial files can be fixed and re-run."),
        ("Comments and blank lines",
         "Lines starting with # (like the ones in the generated template) "
         "and blank lines are ignored."),
    ]),

    ("Example Investor Portfolios", [
        ("Concept",
         "examples/investor_portfolios.csv is a ready-to-run import file "
         "that builds three benchmark/clone portfolios from well-known "
         "investors' public holdings disclosures, so you can compare your "
         "own portfolios against theirs using the same --show-portfolio, "
         "--value-screen, etc. tooling."),
        ("Import it", "code:python3 update_quotes.py --import-file investor_portfolios.csv"),
        ("Berkshire Hathaway (Warren Buffett)",
         "Top 10 holdings by portfolio weight from the actual Q1 2026 13F "
         "filing (period ending 2026-03-31): AAPL, AXP, KO, BAC, CVX, "
         "GOOGL, CB, KHC, DVA, KR. Share counts and reported price are "
         "exactly as filed with the SEC -- not scaled or estimated."),
        ("ARK Innovation ETF / ARKK (Cathie Wood)",
         "Top 10 holdings by weight as of mid-July 2026: TSLA, TEM, AMD, "
         "CRSP, HOOD, SHOP, TWST, COIN, BEAM (SPCX, ARK's #6 holding, was "
         "left out as an unusual thinly-traded instrument that may not "
         "fetch cleanly). Since exact ARKK share counts and its real "
         "~$6B+ fund size aren't practical to reproduce here, this is a "
         "SCALED MODEL: a $1,000,000 notional portfolio allocated in the "
         "same proportions as ARKK's real weights, using approximate "
         "prices to back out share counts."),
        ("Pershing Square Capital Management (Bill Ackman)",
         "Top 5 of 11 total holdings (about 78% of the portfolio) from "
         "the Q1 2026 13F: BN (Brookfield), AMZN, UBER, MSFT, QSR. Also a "
         "SCALED MODEL for the same reason as ARKK -- exact share counts "
         "weren't available from the sources checked, so this uses a "
         "$1,000,000 notional portfolio at the real disclosed weights."),
        ("Refresh with real prices after importing",
         "code:python3 update_quotes.py --portfolio \"Berkshire Hathaway\" \"ARK Innovation ETF (ARKK)\" \"Pershing Square Capital Management\""),
        ("Important caveats", [
            "13F filings are quarterly and delayed up to 45 days by SEC rules, so holdings shown are already somewhat dated the moment they're filed, and drift further as these investors trade.",
            "The ARKK and Pershing Square rows are scaled models, not exact position replication -- treat quantities as illustrative.",
            "This data is for comparison/research purposes only and is not investment advice.",
        ]),
    ]),

    ("SQL Reference -- Portfolio, Group, Value & Fundamentals Queries", [
        ("Open the database directly", "code:sqlite3 /sdcard/Download/stock_reports.db"),

        ("Group: full quote history", [
            "Every quote ever recorded for every symbol in a group, newest first per symbol.",
        ]),
        ("Group history query", "code:SELECT\n    c.symbol,\n    c.name,\n    r.generated_at,\n    q.current_price,\n    q.pe_ratio,\n    q.dividend_yield,\n    q.day_high,\n    q.day_low,\n    q.week52_high,\n    q.week52_low,\n    q.volume,\n    q.market_cap,\n    q.error_message\nFROM quotes q\nJOIN companies c ON c.id = q.company_id\nJOIN reports r ON r.id = q.report_id\nJOIN group_members gm ON gm.company_id = c.id\nJOIN groups g ON g.id = gm.group_id\nWHERE g.name = 'Dow 30'\nORDER BY c.symbol, r.generated_at;"),

        ("Group: latest quote only, per symbol",
         "code:SELECT\n    c.symbol,\n    c.name,\n    q.current_price,\n    q.pe_ratio,\n    q.dividend_yield,\n    q.market_cap,\n    q.volume,\n    r.generated_at\nFROM quotes q\nJOIN companies c ON c.id = q.company_id\nJOIN reports r ON r.id = q.report_id\nJOIN group_members gm ON gm.company_id = c.id\nJOIN groups g ON g.id = gm.group_id\nWHERE g.name = 'Dow 30'\n  AND q.report_id = (\n      SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id\n  )\nORDER BY c.symbol;"),

        ("Group: latest price with change and % gain/loss (pure SQL)",
         "This is the same logic --show-group runs internally, using a window function to line up each symbol's latest quote against its prior one."),
        ("Price change query", "code:WITH ranked AS (\n    SELECT\n        c.symbol,\n        c.name,\n        q.current_price,\n        q.report_id,\n        LAG(q.current_price) OVER (\n            PARTITION BY c.id ORDER BY q.report_id\n        ) AS prev_price,\n        ROW_NUMBER() OVER (\n            PARTITION BY c.id ORDER BY q.report_id DESC\n        ) AS rn\n    FROM quotes q\n    JOIN companies c ON c.id = q.company_id\n    JOIN group_members gm ON gm.company_id = c.id\n    JOIN groups g ON g.id = gm.group_id\n    WHERE g.name = 'Dow 30'\n)\nSELECT\n    symbol,\n    name,\n    current_price,\n    ROUND(current_price - prev_price, 2) AS price_change,\n    ROUND((current_price - prev_price) / prev_price * 100, 2) AS pct_change\nFROM ranked\nWHERE rn = 1\nORDER BY symbol;"),

        ("Group: list every group and its symbol count",
         "code:SELECT g.name, COUNT(gm.company_id) AS symbol_count\nFROM groups g\nLEFT JOIN group_members gm ON gm.group_id = g.id\nGROUP BY g.id\nORDER BY g.name;"),

        ("Group: list the symbols in one group",
         "code:SELECT c.symbol\nFROM group_members gm\nJOIN companies c ON c.id = gm.company_id\nJOIN groups g ON g.id = gm.group_id\nWHERE g.name = 'Dow 30'\nORDER BY c.symbol;"),

        ("Portfolio: holdings with live price and gain/loss",
         "code:SELECT * FROM portfolio_positions\nWHERE portfolio_name = 'Growth'\nORDER BY symbol;"),

        ("Portfolio: total value",
         "code:SELECT * FROM portfolio_totals\nWHERE portfolio_name = 'Growth';"),

        ("Portfolio: raw holdings table (quantity and avg cost only)",
         "code:SELECT c.symbol, h.quantity, h.avg_purchase_price, h.first_purchase_date\nFROM holdings h\nJOIN companies c ON c.id = h.company_id\nJOIN portfolios p ON p.id = h.portfolio_id\nWHERE p.name = 'Growth'\nORDER BY c.symbol;"),

        ("Portfolio: transaction history (with IDs, for editing/deleting)",
         "code:SELECT t.id, t.transaction_date, t.transaction_type, c.symbol,\n       t.quantity, t.price_per_share\nFROM portfolio_transactions t\nJOIN companies c ON c.id = t.company_id\nJOIN portfolios p ON p.id = t.portfolio_id\nWHERE p.name = 'Growth'\nORDER BY t.transaction_date, t.id;"),

        ("Portfolio: list every portfolio and its owner",
         "code:SELECT p.id, p.name, o.name AS owner\nFROM portfolios p\nJOIN portfolio_owners o ON o.id = p.owner_id\nORDER BY o.name, p.name;"),

        ("Value screen: P/E below sector average",
         "This is the same logic --value-screen runs internally. Only "
         "companies with a sane, non-null P/E are included -- some "
         "symbols have P/E stored as literal infinity from near-zero "
         "earnings, and the BETWEEN clause filters those out along with "
         "penny stocks and micro-caps."),
        ("Sector P/E query", "code:WITH latest AS (\n    SELECT q.company_id, c.symbol, c.name, s.name AS sector_name,\n           q.pe_ratio, q.dividend_yield, q.current_price, q.market_cap\n    FROM quotes q\n    JOIN companies c ON c.id = q.company_id\n    LEFT JOIN sectors s ON s.id = c.sector_id\n    WHERE q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id)\n      AND q.pe_ratio IS NOT NULL AND q.pe_ratio BETWEEN 3 AND 60\n      AND q.current_price IS NOT NULL AND q.current_price > 5\n      AND q.market_cap IS NOT NULL AND q.market_cap > 1000000000\n),\nsector_avg AS (\n    SELECT sector_name, AVG(pe_ratio) AS avg_sector_pe, COUNT(*) AS n\n    FROM latest\n    WHERE sector_name IS NOT NULL\n    GROUP BY sector_name\n    HAVING COUNT(*) >= 3\n)\nSELECT\n    l.symbol, l.name, l.sector_name,\n    ROUND(l.pe_ratio, 2) AS pe_ratio,\n    ROUND(sa.avg_sector_pe, 2) AS sector_avg_pe,\n    ROUND((l.pe_ratio - sa.avg_sector_pe) / sa.avg_sector_pe * 100, 1) AS pct_below_sector_pe,\n    l.dividend_yield\nFROM latest l\nJOIN sector_avg sa ON sa.sector_name = l.sector_name\nWHERE l.pe_ratio < sa.avg_sector_pe\nORDER BY pct_below_sector_pe ASC\nLIMIT 25;"),

        ("Value screen: P/E below industry average",
         "Same idea, but grouped by the finer-grained industry instead "
         "of sector."),
        ("Industry P/E query", "code:WITH latest AS (\n    SELECT q.company_id, c.symbol, c.name, i.name AS industry_name,\n           q.pe_ratio, q.dividend_yield, q.current_price, q.market_cap\n    FROM quotes q\n    JOIN companies c ON c.id = q.company_id\n    LEFT JOIN industries i ON i.id = c.industry_id\n    WHERE q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id)\n      AND q.pe_ratio IS NOT NULL AND q.pe_ratio BETWEEN 3 AND 60\n      AND q.current_price IS NOT NULL AND q.current_price > 5\n      AND q.market_cap IS NOT NULL AND q.market_cap > 1000000000\n),\nindustry_avg AS (\n    SELECT industry_name, AVG(pe_ratio) AS avg_industry_pe, COUNT(*) AS n\n    FROM latest\n    WHERE industry_name IS NOT NULL\n    GROUP BY industry_name\n    HAVING COUNT(*) >= 3\n)\nSELECT\n    l.symbol, l.name, l.industry_name,\n    ROUND(l.pe_ratio, 2) AS pe_ratio,\n    ROUND(ia.avg_industry_pe, 2) AS industry_avg_pe,\n    ROUND((l.pe_ratio - ia.avg_industry_pe) / ia.avg_industry_pe * 100, 1) AS pct_below_industry_pe\nFROM latest l\nJOIN industry_avg ia ON ia.industry_name = l.industry_name\nWHERE l.pe_ratio < ia.avg_industry_pe\nORDER BY pct_below_industry_pe ASC\nLIMIT 25;"),

        ("Value screen: trading near its own 52-week low",
         "A different lens from the peer-P/E queries above -- \"beaten "
         "down relative to itself,\" regardless of sector/industry peers. "
         "pct_of_52wk_range near 0 means sitting right at the 52-week "
         "low; near 100 means at the high."),
        ("52-week range query", "code:WITH latest AS (\n    SELECT q.company_id, c.symbol, c.name, s.name AS sector_name,\n           q.current_price, q.week52_high, q.week52_low, q.pe_ratio,\n           q.dividend_yield, q.market_cap\n    FROM quotes q\n    JOIN companies c ON c.id = q.company_id\n    LEFT JOIN sectors s ON s.id = c.sector_id\n    WHERE q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id)\n      AND q.current_price IS NOT NULL AND q.current_price > 5\n      AND q.market_cap IS NOT NULL AND q.market_cap > 1000000000\n      AND q.week52_high IS NOT NULL AND q.week52_low IS NOT NULL AND q.week52_high > q.week52_low\n)\nSELECT\n    symbol, name, sector_name, current_price, week52_low, week52_high,\n    ROUND((current_price - week52_low) * 100.0 / (week52_high - week52_low), 1) AS pct_of_52wk_range,\n    ROUND(pe_ratio, 2) AS pe_ratio, dividend_yield\nFROM latest\nORDER BY pct_of_52wk_range ASC\nLIMIT 25;"),

        ("Value screen: combined -- cheap vs. sector AND near its low",
         "Merges both signals into one screen -- what --value-screen "
         "actually runs."),
        ("Combined screen query", "code:WITH latest AS (\n    SELECT q.company_id, c.symbol, c.name, s.name AS sector_name,\n           q.pe_ratio, q.dividend_yield, q.current_price,\n           q.week52_high, q.week52_low, q.market_cap\n    FROM quotes q\n    JOIN companies c ON c.id = q.company_id\n    LEFT JOIN sectors s ON s.id = c.sector_id\n    WHERE q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id)\n      AND q.pe_ratio IS NOT NULL AND q.pe_ratio BETWEEN 3 AND 60\n      AND q.current_price IS NOT NULL AND q.current_price > 5\n      AND q.market_cap IS NOT NULL AND q.market_cap > 1000000000\n      AND q.week52_high IS NOT NULL AND q.week52_low IS NOT NULL AND q.week52_high > q.week52_low\n),\nscored AS (\n    SELECT *,\n        AVG(pe_ratio) OVER (PARTITION BY sector_name) AS sector_avg_pe,\n        (current_price - week52_low) * 100.0 / (week52_high - week52_low) AS pct_of_52wk_range\n    FROM latest\n)\nSELECT\n    symbol, name, sector_name,\n    ROUND(pe_ratio, 2) AS pe_ratio,\n    ROUND(sector_avg_pe, 2) AS sector_avg_pe,\n    ROUND((pe_ratio - sector_avg_pe) / sector_avg_pe * 100, 1) AS pct_below_sector_pe,\n    ROUND(pct_of_52wk_range, 1) AS pct_of_52wk_range,\n    dividend_yield\nFROM scored\nWHERE pe_ratio < sector_avg_pe\nORDER BY pct_below_sector_pe ASC, pct_of_52wk_range ASC\nLIMIT 25;"),

        ("Fundamentals: latest snapshot for one symbol",
         "code:SELECT r.generated_at, f.*\nFROM fundamentals f\nJOIN companies c ON c.id = f.company_id\nJOIN reports r ON r.id = f.report_id\nWHERE c.symbol = 'NVDA'\nORDER BY f.report_id DESC\nLIMIT 1;"),

        ("Deep value screen: low P/B, strong ROE, manageable debt, reasonable PEG",
         "This is the same logic --deep-value-screen runs internally, "
         "joining each company's latest fundamentals row with its latest "
         "quote."),
        ("Deep value screen query", "code:WITH latest_q AS (\n    SELECT q.company_id, q.current_price, q.market_cap, q.pe_ratio\n    FROM quotes q\n    WHERE q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id)\n),\nlatest_f AS (\n    SELECT f.company_id, f.price_to_book, f.return_on_equity, f.debt_to_equity,\n           f.peg_ratio, f.profit_margins, f.revenue_growth\n    FROM fundamentals f\n    WHERE f.report_id = (SELECT MAX(f2.report_id) FROM fundamentals f2 WHERE f2.company_id = f.company_id)\n)\nSELECT c.symbol, c.name, s.name AS sector_name,\n       lq.current_price, lq.market_cap, lq.pe_ratio,\n       lf.price_to_book, lf.return_on_equity, lf.debt_to_equity,\n       lf.peg_ratio, lf.profit_margins, lf.revenue_growth\nFROM latest_q lq\nJOIN latest_f lf ON lf.company_id = lq.company_id\nJOIN companies c ON c.id = lq.company_id\nLEFT JOIN sectors s ON s.id = c.sector_id\nWHERE lq.current_price IS NOT NULL AND lq.current_price > 5\n  AND lq.market_cap IS NOT NULL AND lq.market_cap > 1000000000\n  AND lf.price_to_book IS NOT NULL AND lf.price_to_book > 0 AND lf.price_to_book <= 3\n  AND lf.return_on_equity IS NOT NULL AND lf.return_on_equity >= 0.10\n  AND (lf.debt_to_equity IS NULL OR lf.debt_to_equity <= 150)\n  AND (lf.peg_ratio IS NULL OR (lf.peg_ratio > 0 AND lf.peg_ratio <= 2))\nORDER BY lf.price_to_book ASC, lf.return_on_equity DESC\nLIMIT 25;"),

        ("Quality screen: highest ROE among profitable, reasonably-valued companies",
         "A different cut on the same data -- ranks by profitability "
         "quality first (ROE) rather than cheapness first, still bounded "
         "to a sane P/B and market cap so it's not dominated by "
         "micro-caps or balance-sheet outliers."),
        ("ROE quality screen query", "code:WITH latest_q AS (\n    SELECT q.company_id, q.current_price, q.market_cap\n    FROM quotes q\n    WHERE q.report_id = (SELECT MAX(q2.report_id) FROM quotes q2 WHERE q2.company_id = q.company_id)\n),\nlatest_f AS (\n    SELECT f.company_id, f.price_to_book, f.return_on_equity,\n           f.profit_margins, f.revenue_growth, f.debt_to_equity\n    FROM fundamentals f\n    WHERE f.report_id = (SELECT MAX(f2.report_id) FROM fundamentals f2 WHERE f2.company_id = f.company_id)\n)\nSELECT c.symbol, c.name,\n       ROUND(lf.return_on_equity * 100, 1) AS roe_pct,\n       ROUND(lf.profit_margins * 100, 1) AS profit_margin_pct,\n       ROUND(lf.revenue_growth * 100, 1) AS revenue_growth_pct,\n       lf.price_to_book, lf.debt_to_equity, lq.current_price\nFROM latest_q lq\nJOIN latest_f lf ON lf.company_id = lq.company_id\nJOIN companies c ON c.id = lq.company_id\nWHERE lq.current_price IS NOT NULL AND lq.market_cap > 1000000000\n  AND lf.return_on_equity IS NOT NULL AND lf.return_on_equity > 0\n  AND lf.price_to_book IS NOT NULL AND lf.price_to_book BETWEEN 0 AND 8\nORDER BY lf.return_on_equity DESC\nLIMIT 25;"),

        ("Financial health filter: strong liquidity, low leverage",
         "Useful as a pre-filter before applying any value screen -- "
         "surfaces companies that could better weather a downturn: "
         "current ratio and quick ratio both above 1 (more current "
         "assets than current liabilities) and modest debt/equity."),
        ("Liquidity/leverage query", "code:SELECT c.symbol, c.name,\n       f.current_ratio, f.quick_ratio, f.debt_to_equity,\n       ROUND(f.profit_margins * 100, 1) AS profit_margin_pct\nFROM fundamentals f\nJOIN companies c ON c.id = f.company_id\nWHERE f.report_id = (SELECT MAX(f2.report_id) FROM fundamentals f2 WHERE f2.company_id = f.company_id)\n  AND f.current_ratio IS NOT NULL AND f.current_ratio >= 1.5\n  AND f.quick_ratio IS NOT NULL AND f.quick_ratio >= 1.0\n  AND f.debt_to_equity IS NOT NULL AND f.debt_to_equity <= 75\nORDER BY f.current_ratio DESC\nLIMIT 25;"),
    ]),

    ("Reliability Features", [
        ("Automatic retry with backoff",
         "Yahoo Finance occasionally returns transient errors (e.g. HTTP "
         "500) under repeated requests. Each symbol gets up to 3 attempts, "
         "waiting 2s, then 4s, then 8s between tries before giving up and "
         "logging it as an error for that symbol."),
        ("Delay between symbols",
         "A 1-second pause between each symbol request reduces how often "
         "Yahoo's rate limiting triggers in the first place."),
        ("Tuning",
         "MAX_RETRIES, RETRY_BASE_DELAY_SECONDS, and REQUEST_DELAY_SECONDS "
         "are constants near the top of update_quotes.py if these need "
         "adjusting."),
    ]),

    ("Output Control", [
        ("Text report file (currently disabled)",
         "By default the script only writes to the database. A "
         "WRITE_TXT_REPORT flag near the top of the file controls whether "
         "a stock_report_<timestamp>.txt file is also written to the SD "
         "card on each run."),
        ("Re-enabling text reports", "code:WRITE_TXT_REPORT = True   # change this line, near the top of the script"),
    ]),

    ("Full Command Reference", [
        ("table", [
            ["Command", "What it does"],

            ["### Fetching Quotes", ""],
            ["python3 update_quotes.py", "Refresh every symbol already tracked (no arguments)"],
            ["python3 update_quotes.py AAPL MSFT GOOGL", "Fetch specific symbols (space-separated)"],
            ["--group \"NAME\"", "Fetch every symbol in a group"],
            ["--sector \"NAME\"", "Fetch every tracked symbol in a sector"],
            ["--industry \"NAME\"", "Fetch every tracked symbol in an industry"],
            ["--exchange \"NAME\"", "Fetch every tracked symbol on an exchange"],
            ["--portfolio \"NAME\" [\"NAME\" ...]", "Fetch quotes for symbols currently held across one or more portfolios (combine with --owner)"],

            ["### Groups", ""],
            ["--create-group NAME", "Create an empty group"],
            ["--add-to-group NAME SYM...", "Add one or more symbols to a group (creates it if needed)"],
            ["--remove-from-group NAME SYM...", "Remove one or more symbols from a group"],
            ["--list-groups", "List every group and its symbol count"],
            ["--list-group NAME", "List the symbols in one group"],
            ["--show-group NAME", "Latest price, change, % gain/loss, and market cap for every symbol in a group"],
            ["--seed-dow30", "Populate a built-in \"Dow 30\" group"],
            ["--seed-sp500", "Download and populate a current \"S&P 500\" group"],

            ["### Portfolios", ""],
            ["--create-portfolio NAME --owner OWNER", "Create a portfolio for one of the 4 owners"],
            ["--buy NAME SYM QTY PRICE", "Log a buy transaction (combine with --date)"],
            ["--sell NAME SYM QTY PRICE", "Log a sell transaction (combine with --date)"],
            ["--list-portfolios", "List every portfolio and its owner"],
            ["--show-portfolio NAME [NAME ...]", "Show holdings, gain/loss, and total value for one or more portfolios, plus a grand total (combine with --owner)"],
            ["--delete-portfolio NAME", "Delete a portfolio and all its transactions/holdings, whether or not it has any data (combine with --owner)"],

            ["### Portfolio Transactions", ""],
            ["--list-transactions NAME", "List a portfolio's transactions with their IDs (combine with --owner)"],
            ["--edit-transaction ID", "Edit a transaction (combine with --type/--quantity/--price/--date); holdings recalculate automatically"],
            ["--delete-transaction ID", "Delete a transaction; holdings recalculate automatically"],

            ["### Companies", ""],
            ["--add-company SYMBOL", "Add a company record by hand (combine with --company-name/--industry/--sector/--exchange)"],
            ["--edit-company SYMBOL", "Edit an existing company's fields (only what you pass changes)"],
            ["--show-company SYMBOL", "Show a company's details and latest quote"],
            ["--show-symbol SYMBOL", "Show a symbol's quote data; defaults to the latest quote (combine with --full-history or --start-date/--end-date)"],
            ["--show-fundamentals SYMBOL", "Show a symbol's fundamentals data (P/B, ROE, debt/equity, margins, cash flow, growth); defaults to the latest snapshot"],
            ["--delete-company SYMBOL", "Delete a company and every quote, group membership, holding, and transaction that references it"],

            ["### Value Screening", ""],
            ["--value-screen", "Screen for stocks below their sector/industry average P/E, ranked by discount and proximity to 52-week low"],
            ["--group-by sector|industry", "Peer group for --value-screen (default: sector)"],
            ["--scope NAME", "Limit --value-screen results to one sector/industry name"],
            ["--min-pe N / --max-pe N", "P/E bounds for --value-screen (default: 3 / 60)"],
            ["--min-price N", "Minimum share price for value screens (default: 5)"],
            ["--min-market-cap N", "Minimum market cap for value screens (default: 1000000000)"],
            ["--limit N", "Max results for value screens (default: 25)"],
            ["--deep-value-screen", "Fundamentals-based screen: low price-to-book, strong ROE, manageable debt/equity, reasonable PEG"],
            ["--max-pb N", "Maximum price-to-book for --deep-value-screen (default: 3)"],
            ["--min-roe N", "Minimum return on equity, as a fraction, for --deep-value-screen (default: 0.10)"],
            ["--max-debt-equity N", "Maximum debt-to-equity for --deep-value-screen (default: 150)"],
            ["--max-peg N", "Maximum PEG ratio for --deep-value-screen (default: 2)"],

            ["### Bulk Import from a File", ""],
            ["--import-template PATH", "Write a starter CSV file with headers, docs, and example rows for every action"],
            ["--import-file PATH", "Apply a CSV file of buy/sell/edit/delete transactions, portfolio deletes, group changes, and company add/edit/delete"],

            ["### PDF Export", ""],
            ["--pdf NAME", "Also write --show-portfolio/--show-symbol/--show-fundamentals/--show-group output to a PDF, saved under /sdcard/documents/ with a timestamp appended"],
            ["--fundamentals", "With --show-portfolio/--show-group, show fundamentals metrics per symbol instead of price/gain-loss (combine with --fields/--pdf)"],

            ["### Shared Options", ""],
            ["--owner NAME", "Disambiguate a portfolio by owner (see PORTFOLIO_OWNERS in the script for the full list)"],
            ["--date YYYY-MM-DD", "Date for --buy/--sell/--edit-transaction (defaults to today for --buy/--sell)"],
            ["--type BUY|SELL", "New transaction type, for --edit-transaction"],
            ["--quantity QTY", "New quantity, for --edit-transaction"],
            ["--price PRICE", "New price per share, for --edit-transaction"],
            ["--company-name NAME", "Company name, for --add-company/--edit-company"],
            ["--full-history", "With --show-symbol/--show-fundamentals, show every record instead of just the latest"],
            ["--start-date YYYY-MM-DD", "With --show-symbol/--show-fundamentals, only show records on/after this date"],
            ["--end-date YYYY-MM-DD", "With --show-symbol/--show-fundamentals, only show records on/before this date"],
            ["--fields FIELD [FIELD ...]", "With --show-symbol/--show-fundamentals, only show these fields (space- or comma-separated)"],
        ]),
    ]),
]


def build_pdf():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    doc = SimpleDocTemplate(
        OUTPUT_PATH, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("DocTitle", parent=styles["Title"], fontSize=22, spaceAfter=4)
    subtitle_style = ParagraphStyle("DocSubtitle", parent=styles["Normal"], fontSize=10, textColor=colors.grey, spaceAfter=24)
    section_style = ParagraphStyle("SectionHeading", parent=styles["Heading1"], fontSize=16, spaceBefore=18, spaceAfter=10, textColor=colors.HexColor("#1a3d5c"))
    subheading_style = ParagraphStyle("SubHeading", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#333333"))
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, leading=14, spaceAfter=6)
    bullet_style = ParagraphStyle("Bullet", parent=styles["Normal"], fontSize=10, leading=14)
    code_style = ParagraphStyle(
        "Code", parent=styles["Normal"], fontName="Courier", fontSize=9, leading=13,
        backColor=colors.HexColor("#f2f2f2"), borderPadding=6, spaceAfter=8, spaceBefore=2,
    )

    story = []
    story.append(Paragraph("update_quotes.py -- Feature Documentation", title_style))
    story.append(Paragraph(f"Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')}", subtitle_style))

    for section_title, items in SECTIONS:
        story.append(Paragraph(section_title, section_style))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#1a3d5c"), spaceAfter=8))

        for heading, content in items:
            if heading == "table":
                table_header_style = ParagraphStyle("TableHeader", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=colors.white)
                table_category_style = ParagraphStyle("TableCategory", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=colors.HexColor("#1a3d5c"))
                table_cmd_style = ParagraphStyle("TableCmd", parent=styles["Normal"], fontName="Courier", fontSize=8, leading=10)
                table_desc_style = ParagraphStyle("TableDesc", parent=styles["Normal"], fontSize=8.5, leading=11)

                display_rows = []
                category_row_indexes = []
                for row_index, row in enumerate(content):
                    if row_index == 0:
                        display_rows.append([Paragraph(cell, table_header_style) for cell in row])
                    elif row and isinstance(row[0], str) and row[0].startswith("### "):
                        category_row_indexes.append(row_index)
                        display_rows.append([Paragraph(row[0][4:], table_category_style), ""])
                    else:
                        display_rows.append([
                            Paragraph(row[0], table_cmd_style),
                            Paragraph(row[1], table_desc_style),
                        ])

                table = Table(display_rows, colWidths=[2.5 * inch, 4.5 * inch])
                style_commands = [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3d5c")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
                for row_index in category_row_indexes:
                    style_commands.extend([
                        ("SPAN", (0, row_index), (-1, row_index)),
                        ("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#dbe4ec")),
                    ])
                table.setStyle(TableStyle(style_commands))
                story.append(table)
                continue

            story.append(Paragraph(heading, subheading_style))

            if isinstance(content, str) and content.startswith("code:"):
                code_text = content[len("code:"):].replace("\n", "<br/>")
                story.append(Paragraph(code_text, code_style))
            elif isinstance(content, str):
                story.append(Paragraph(content, body_style))
            elif isinstance(content, list):
                bullets = []
                for line in content:
                    if line.startswith("python3 ") or "--" in line[:2]:
                        line_html = f'<font face="Courier" size="9">{line}</font>'
                    else:
                        line_html = line
                    bullets.append(ListItem(Paragraph(line_html, bullet_style), leftIndent=12))
                story.append(ListFlowable(bullets, bulletType="bullet", start="•"))
                story.append(Spacer(1, 6))

        story.append(Spacer(1, 4))

    doc.build(story)
    print(f"Documentation PDF written to: {OUTPUT_PATH}")


if __name__ == "__main__":
    build_pdf()
