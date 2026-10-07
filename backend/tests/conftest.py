"""Shared fixtures: synthetic assets (generated once, cached by version) and a temp database."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "tests_out"
sys.path.insert(0, str(ROOT / "tools"))

os.environ.setdefault("DATA_DIR", str(ROOT / "tests_out" / "data"))
os.environ.setdefault("EXPORTS_DIR", str(ROOT / "tests_out" / "exports"))
os.environ.setdefault("INBOX_DIR", str(ROOT / "tests_out" / "inbox"))


@pytest.fixture(scope="session")
def assets() -> dict:
    import make_test_assets as gen

    gt_path = ASSETS / "ground_truth.json"
    if gt_path.exists():
        gt = json.loads(gt_path.read_text())
        if gt.get("version") == gen.ASSET_VERSION and all((ASSETS / c["path"]).exists() for c in gt["clips"].values()):
            gt["dir"] = str(ASSETS)
            return gt
    gt = gen.main(ASSETS)
    gt["dir"] = str(ASSETS)
    return gt


@pytest.fixture()
def db(tmp_path):
    from wildcut import db as dbmod

    engine = dbmod.reset_engine_for_tests(tmp_path / "test.db")
    yield engine
    dbmod._engine = None
