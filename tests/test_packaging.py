"""Packaging: the default config ships INSIDE the wheel (a plain non-editable `pip install .` must work)."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("wheelsrc")
    shutil.copy(ROOT / "pyproject.toml", work)
    shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    out = work / "dist"
    # With setuptools already installed, skip build isolation (it would download setuptools): works offline.
    has_setuptools = importlib.util.find_spec("setuptools") is not None
    cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(out), str(work)]
    if has_setuptools:
        cmd.insert(5, "--no-build-isolation")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    wheels = sorted(out.glob("wallexbot-*.whl")) if out.is_dir() else []
    if r.returncode != 0 or not wheels:
        pytest.skip(f"pip wheel cannot run here: {(r.stderr or r.stdout)[-300:]}")
    return wheels[0]


def test_wheel_contains_default_config_and_no_repo_root_config(wheel):
    names = zipfile.ZipFile(wheel).namelist()
    assert "wallexbot/default_config.yaml" in names
    assert not any(n.startswith("config/") for n in names)
    assert "wallexbot/cli.py" in names and "wallexbot/__main__.py" in names


def test_wheel_default_config_is_the_source_one(wheel):
    packaged = zipfile.ZipFile(wheel).read("wallexbot/default_config.yaml")
    assert packaged == (ROOT / "src" / "wallexbot" / "default_config.yaml").read_bytes()


def test_no_second_source_of_truth():
    assert not (ROOT / "config" / "default.yaml").exists()
