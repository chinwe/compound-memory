"""embedding 模型缝的配置解析测试（不依赖 onnxruntime/真模型）。

通用化约束的守卫：模型 repo id 与维度默认值不可漂移（宿主升级行为不变），
环境变量可换模型（换模型属运维动作，这是它的代码入口）；缓存目录推导必须
遵循 HF hub 的 models--<org>--<name> 命名规则，否则换模型后在缓存里找不到。
"""

import importlib
from pathlib import Path

import pytest

from compound_memory import embedding


def test_default_model_and_dim_unchanged() -> None:
    """默认值是向后兼容契约：未设环境变量时所有宿主行为与 BGE-small-zh 时代一致。"""
    assert embedding.MODEL_REPO_ID == "Xenova/bge-small-zh-v1.5"
    assert embedding.EMBED_DIM == 512
    assert embedding._MODEL_GLOB == (
        ".cache/huggingface/hub/models--Xenova--bge-small-zh-v1.5/snapshots/*"
    )


def test_cache_glob_follows_hf_naming() -> None:
    """缓存目录推导：repo id 的 '/' 替换为 '--'；多段 repo id 同样成立。"""
    assert (
        embedding._cache_glob("BAAI/bge-m3")
        == ".cache/huggingface/hub/models--BAAI--bge-m3/snapshots/*"
    )
    assert (
        embedding._cache_glob("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        == ".cache/huggingface/hub/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/snapshots/*"
    )


def test_env_override_resolves_model_and_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量换模型：reload 后 repo id/维度/glob 三者一致指向新模型。

    reload 只在本测试生效（finally 恢复），避免污染其他测试的模块状态。
    """
    monkeypatch.setenv("COMPOUND_MEMORY_EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("COMPOUND_MEMORY_EMBEDDING_DIM", "1024")
    try:
        mod = importlib.reload(embedding)
        assert mod.MODEL_REPO_ID == "BAAI/bge-m3"
        assert mod.EMBED_DIM == 1024
        assert mod._MODEL_GLOB == ".cache/huggingface/hub/models--BAAI--bge-m3/snapshots/*"
    finally:
        importlib.reload(embedding)


@pytest.mark.skipif(not embedding.VEC_AVAILABLE, reason="vec extra not installed")
def test_missing_model_reports_repo_id(tmp_path: Path) -> None:
    """模型缺失的降级路径：报错消息含当前 repo id，提示运维该装哪个模型。"""
    with pytest.raises(embedding.ModelMissingError) as exc_info:
        embedding.BgeEncoder(home=tmp_path)
    assert embedding.MODEL_REPO_ID in str(exc_info.value)
