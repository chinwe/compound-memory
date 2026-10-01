"""Test config.

Why we don't use pytest's builtin tmp_path: the WorkBuddy sandbox (a) raises
EEXIST on mkdir of an existing dir and (b) blocks bulk unlinks via its trash
hook. Our fixtures create fresh project-local dirs instead.

The asyncio-first ordering keeps mcp.Client sessions away from tests that
monkeypatch shutil.which (anyio cancel-scope runs in a different task otherwise).

Seam adapters injected by the shared store fixture:
- clock: fixed date, so decay/rank assertions never depend on the wall clock
  (midnight-crossing flakes).
- remover: rename instead of unlink, so archive/revive during tests stay
  sandbox-safe (production uses plain Path.unlink — single-file unlink is fine,
  only bulk deletes get blocked).
"""

import datetime as dt
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from compound_memory.storage import MemoryStore  # noqa: E402

_TEST_TMP_BASE = Path(__file__).resolve().parents[1] / ".test-tmp"

# 测试用的固定"今天"：store fixture 的 clock 与各测试文件的日期推算都相对它
CLOCK_DATE = dt.date(2026, 10, 1)


def sandbox_safe_remove(path: Path) -> None:
    """Removal adapter for the sandbox: rename out of the way instead of unlink."""
    if path.exists():
        os.replace(path, path.with_name(f".{path.name}.rm"))


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


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    """共享的 MemoryStore fixture（原先在 test_lifecycle / test_index 各有一份）。

    注入固定 clock 与沙箱安全 remover——两条 seam adapter 都只在测试侧存在。
    """
    return MemoryStore(tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
