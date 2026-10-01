import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_BASE = Path(__file__).resolve().parents[1] / ".test-tmp"


def pytest_configure(config):
    if not _BASE.exists():
        _BASE.mkdir()


def pytest_collection_modifyitems(session, config, items):
    def is_async(item):
        return "asyncio" in item.keywords

    items.sort(key=lambda it: 0 if is_async(it) else 1)


import pytest  # noqa: E402


@pytest.fixture
def tmp_path_factory():
    class _Factory:
        def mktemp(self, name: str, numbered: bool = True) -> Path:
            d = _BASE / f"{name}-{uuid.uuid4().hex[:8]}"
            d.mkdir(parents=True, exist_ok=True)
            return d

        def getbasetemp(self) -> Path:
            return _BASE

    return _Factory()


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("t")

