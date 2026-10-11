"""default_root：COMPOUND_MEMORY_ROOT 的用户目录展开与空白回退。

文档与宿主 JSON 常把记忆库根写成 ``~/.agents/memory``。覆盖值必须落到真实
home，未设置或空白则回退默认路径——字面量 ``~`` 目录会和默认库分裂。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from compound_memory.storage import default_root

_DEFAULT = Path.home() / ".agents" / "memory"


def test_tilde_env_resolves_under_real_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """COMPOUND_MEMORY_ROOT=~/.agents/memory 落到真实 home，不是 cwd 下的字面量 ~。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPOUND_MEMORY_ROOT", "~/.agents/memory")
    root = default_root()
    assert root == _DEFAULT
    assert root.is_absolute()
    assert "~" not in root.parts
    assert not root.is_relative_to(tmp_path)


def test_padded_tilde_env_resolves_under_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """两侧空白仍展开，避免 `` ~/.agents/memory `` 落成字面量路径。"""
    monkeypatch.setenv("COMPOUND_MEMORY_ROOT", "  ~/.agents/memory\n")
    assert default_root() == _DEFAULT


def test_unset_env_uses_home_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPOUND_MEMORY_ROOT", raising=False)
    assert default_root() == _DEFAULT


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_blank_override_falls_back_to_home_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("COMPOUND_MEMORY_ROOT", raw)
    assert default_root() == _DEFAULT


@pytest.mark.parametrize("raw", ["$HOME/.agents/memory", "${HOME}/.agents/memory"])
def test_home_var_prefix_resolves_under_real_home(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """JSON env 块不经 shell，$HOME 前缀仍要落到真实 home。"""
    monkeypatch.setenv("COMPOUND_MEMORY_ROOT", raw)
    assert default_root() == _DEFAULT


def test_tilde_follows_pinned_platform_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """~ 跟着平台 home，不跟着「另一个」家目录变量。

    POSIX 的 expanduser / Path.home() 读 HOME。Windows 先读 USERPROFILE
    （没有才拼 HOMEDRIVE+HOMEPATH），不读 HOME——只改 HOME 会把记忆库写进真实用户目录。
    """
    pinned = tmp_path / "home"
    other = tmp_path / "other"
    pinned.mkdir()
    other.mkdir()
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(pinned))
        drive, tail = os.path.splitdrive(str(pinned))
        monkeypatch.setenv("HOMEDRIVE", drive)
        monkeypatch.setenv("HOMEPATH", tail or "\\")
        monkeypatch.setenv("HOME", str(other))
    else:
        monkeypatch.setenv("HOME", str(pinned))
        monkeypatch.setenv("USERPROFILE", str(other))
    monkeypatch.setenv("COMPOUND_MEMORY_ROOT", "~/.agents/memory")
    assert default_root() == pinned / ".agents" / "memory"


def test_absolute_override_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COMPOUND_MEMORY_ROOT", str(tmp_path))
    assert default_root() == tmp_path


def test_relative_override_stays_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOUND_MEMORY_ROOT", "custom/store")
    assert default_root() == Path("custom/store")
