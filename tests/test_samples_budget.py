"""The committed samples stay small and redistributable: under 5 MB, and trimmed text only.

A posting's zip is kept locally to re-trim from (gitignored), never committed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUDGET_BYTES = 5 * 1024 * 1024


def _tracked_samples() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "samples"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / line for line in out.splitlines() if line]


def test_samples_are_small_and_hold_no_zips() -> None:
    files = _tracked_samples()
    assert files
    assert not [f.name for f in files if f.suffix == ".zip"]
    assert sum(f.stat().st_size for f in files) < BUDGET_BYTES
