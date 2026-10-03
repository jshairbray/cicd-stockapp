"""
Run update_quotes.py's CLI directly on the VM, pointed at the real data
directory instead of its Android-default /sdcard paths.

Usage (same flags as update_quotes.py itself):
    ./venv/bin/python3 cli.py --list-portfolios
    ./venv/bin/python3 cli.py AAPL MSFT --pdf report.pdf
"""
import os
import sys

import update_quotes as uq

DATA_DIR = os.environ.get("STOCKAPP_DATA_DIR", "/opt/stockapp/data")
uq.OUTPUT_DIR = DATA_DIR + "/"
uq.DB_PATH = os.path.join(DATA_DIR, "stock_reports.db")
uq.PDF_OUTPUT_DIR = os.path.join(DATA_DIR, "pdfs") + "/"
os.makedirs(uq.PDF_OUTPUT_DIR, exist_ok=True)

if __name__ == "__main__":
    sys.argv[0] = "update_quotes.py"
    uq.main()
