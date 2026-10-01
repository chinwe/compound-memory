"""Test config.

Why we don't use pytest's builtin tmp_path: the WorkBuddy sandbox (a) raises
EEXIST on mkdir of an existing dir and (b) blocks bulk unlinks via its trash
hook. Our fixtures create fresh project-local dirs instead.

The asyncio-first ordering keeps mcp.Client sessions away from tests that
monkeypatch shutil.which (anyio cancel-scope runs in a different task otherwise).
"""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_TEST_TMP_BASE = Path(__file__).resolve().parents[1] / ".test-tmp"


def pytest_configure(config):
    if not _TEST_TMP_BASE.exists():
        _TEST_TMP_BASE.mkdir()


def pytest_collection_modifyitems(session, config, items):
    items.sort(key=lambda it: 0 if "asyncio" in it.keywords else 1)


@pytest.fixture
def tmp_path_factory():
    class _TestTmpFactory:
        def mktemp(self, name: str, numbered: bool = True) -> Path:
            d = _TEST_TMP_BASE / f"{name}-{uuid.uuid4().hex[:8]}"
            d.mkdir(parents=True, exist_ok=True)
            return d

        def getbasetemp(self) -> Path:
            return _TEST_TMP_BASE

    return _TestTmpFactory()


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("t")
