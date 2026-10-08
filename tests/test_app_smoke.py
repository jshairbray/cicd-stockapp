"""Smoke test: the Streamlit app starts and renders without an exception."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """An empty data folder with a fresh database, like a brand-new VM."""
    (tmp_path / "pdfs").mkdir()
    env = {**os.environ, "STOCKAPP_DATA_DIR": str(tmp_path)}
    subprocess.run(
        [sys.executable, "cli.py", "--list-groups"],
        cwd=REPO, env=env, check=True, timeout=120,
    )
    monkeypatch.setenv("STOCKAPP_DATA_DIR", str(tmp_path))
    return tmp_path


def test_app_starts(data_dir):
    at = AppTest.from_file(str(REPO / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception, [e.message for e in at.exception]
