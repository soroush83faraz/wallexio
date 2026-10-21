import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fx():
    """Load a fixture as parsed JSON (plain floats are fine here; tests re-serialise to bytes)."""
    def load(name: str):
        return json.loads((FIXTURES / name).read_text())
    return load


@pytest.fixture
def fx_bytes():
    def load(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()
    return load


@pytest.fixture
def fetched_at() -> int:
    """Unix time at which the fixtures were captured; the newest 1m/1h bars were still forming."""
    return int((FIXTURES / "FETCHED_AT.txt").read_text().strip())
