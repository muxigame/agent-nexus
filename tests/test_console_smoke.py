"""Browser smoke of the web consoles (scripts/console_screens.py --smoke).

Needs Playwright and Microsoft Edge; skipped otherwise. Run it with
``uv run --with playwright pytest tests/test_console_smoke.py`` (no browser
download: Playwright drives the installed Edge, ``channel="msedge"``).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "console_screens.py"


def test_console_pages_load_without_errors_or_overflow():
    result = subprocess.run([sys.executable, str(SCRIPT), "--smoke"], capture_output=True, text=True, timeout=600,
                            encoding="utf-8", errors="replace")
    if "Executable doesn't exist" in result.stderr or "msedge" in result.stderr and "not found" in result.stderr:
        pytest.skip("Microsoft Edge is not installed")
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "smoke: 0 problem(s)" in result.stdout
