"""测试配置。

为何不用 pytest 内置 tmp_path：WorkBuddy 沙箱 (a) 对已存在目录 mkdir 报 EEXIST，
(b) 通过 trash hook 拦截批量 unlink。fixture 改为新建项目本地目录。

共享 store fixture 注入的 seam adapter：
- clock：固定日期，decay/rank 断言不依赖墙钟（消除跨零点抖动）。
- remover：改名代替 unlink，测试中的归档/复活保持沙箱安全
  （生产用普通 Path.unlink——单文件 unlink 不受影响，只有批量删除会被拦）。
"""

import datetime as dt
import math
import os
import shutil
import sys
import uuid
import zlib
from pathlib import Path
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from compound_memory.scoring import tokenize  # noqa: E402
from compound_memory.storage import MemoryStore  # noqa: E402

_TEST_TMP_BASE = Path(__file__).resolve().parents[1] / ".test-tmp"

# 测试用的固定"今天"：store fixture 的 clock 与各测试文件的日期推算都相对它
CLOCK_DATE = dt.date(2026, 10, 1)


def sandbox_safe_remove(path: Path) -> None:
    """沙箱安全删除 adapter：改名挪走，不做 unlink。"""
    if path.exists():
        os.replace(path, path.with_name(f".{path.name}.rm"))


def _prune_test_tmp() -> None:
    """上轮残留的 fixture 目录整目录改名挪走再删——避免逐个 unlink（沙箱拦批量删除）。

    删除被沙箱拦截时只留一个 .test-tmp.previous 目录（不随运行次数增长），下轮再试；
    挪不动（如并发运行）则维持原状，不阻塞测试。
    """
    previous = _TEST_TMP_BASE.with_name(".test-tmp.previous")
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)
    if not _TEST_TMP_BASE.exists():
        return
    try:
        os.replace(_TEST_TMP_BASE, previous)
    except OSError:
        return
    shutil.rmtree(previous, ignore_errors=True)


def pytest_configure(config):
    _prune_test_tmp()
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
    不带 embedder：默认走纯词面，向量行为全部由 vec_store 显式覆盖。
    """
    return MemoryStore(tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)


def bag_embedder_factory(dim: int = 512) -> Callable[[list[str]], list[list[float]]]:
    """确定性词袋 embedder：token hash 落维后 L2 归一——余弦 ≈ 词集重叠率。

    让向量测试"语义相近"可控且不依赖 onnxruntime/真模型；由 zlib.crc32 保证跨运行稳定。
    """

    def embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * dim
            for tok in set(tokenize(text)):
                vec[zlib.crc32(tok.encode("utf-8")) % dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    return embed


@pytest.fixture
def bag_embedder() -> Callable[[list[str]], list[list[float]]]:
    return bag_embedder_factory()


@pytest.fixture
def vec_store(tmp_path: Path, bag_embedder: Callable[[list[str]], list[list[float]]]) -> MemoryStore:
    """带向量路的 store：其余 seam 与 store fixture 一致。"""
    return MemoryStore(
        tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove, embedder=bag_embedder
    )
