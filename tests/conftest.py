"""测试配置。

为何不用 pytest 内置 tmp_path：WorkBuddy 沙箱 (a) 对已存在目录 mkdir 报 EEXIST，
(b) 通过 trash hook 拦截批量 unlink。fixture 改为新建项目本地目录。

共享 store fixture 注入的 seam adapter：
- clock：固定日期，decay/rank 断言不依赖墙钟（消除跨零点抖动）。
- remover：改名代替 unlink，测试中的归档/复活保持沙箱安全
  （生产用普通 Path.unlink——单文件 unlink 不受影响，只有批量删除会被拦）。
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
    """沙箱安全删除 adapter：改名挪走，不做 unlink。"""
    if path.exists():
        os.replace(path, path.with_name(f".{path.name}.rm"))


def pytest_configure(config):
    if not _TEST_TMP_BASE.exists():
        _TEST_TMP_BASE.mkdir()


@pytest.fixture
def tmp_path_factory():
    class _TestTmpFactory:
        def mktemp(self, name: str) -> Path:
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
