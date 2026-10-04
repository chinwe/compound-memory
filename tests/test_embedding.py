"""embedding 模型缝的测试（配置解析守卫 + encode 分块行为，均不依赖真模型）。

配置守卫：模型 repo id 与维度默认值不可漂移（宿主升级行为不变），
环境变量可换模型（换模型属运维动作，这是它的代码入口）；缓存目录推导必须
遵循 HF hub 的 models--<org>--<name> 命名规则，否则换模型后在缓存里找不到。

encode 缝（issue #16）：onnxruntime 是外部 driver，观察发给它的 batch 形状
是正当缝——大输入必须分块成有界 batch（单次巨批 run 是 LongMemEval 灌库
40+ 分钟的根因），且分块拼接后输出与输入逐条对应、L2 归一化不被分块破坏。
对调用方 encode 仍是一次全量调用（分块是批量面优化，与增量索引无关）。
"""

import importlib
from pathlib import Path
from types import SimpleNamespace

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


# ---------- encode 分块缝（fake session 驱动，不碰真模型；issue #16） ----------


class _FakeEncoding:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        self.attention_mask = [1] * len(ids)
        self.type_ids = [0] * len(ids)


class _FakeTokenizer:
    """ids 长度随文本变化（模拟真实 padding 语义）；记录调用次数。"""

    def __init__(self) -> None:
        self.calls = 0

    def enable_truncation(self, max_length: int) -> None:
        pass

    def enable_padding(self) -> None:
        pass

    def encode_batch(self, texts: list[str]) -> list[_FakeEncoding]:
        self.calls += 1
        return [_FakeEncoding([len(t), 7, 3]) for t in texts]


class _FakeSession:
    """记录每次 run 的 batch 大小；[CLS] 第 0 维写全局行号，跨块顺序可断言。"""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []
        self._seen = 0

    def get_inputs(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(name="input_ids"),
            SimpleNamespace(name="attention_mask"),
            SimpleNamespace(name="token_type_ids"),
        ]

    def run(self, _names, feed: dict):
        import numpy as np

        ids = feed["input_ids"]
        self.batch_sizes.append(int(ids.shape[0]))
        out = np.zeros((ids.shape[0], ids.shape[1], 2), dtype=np.float32)
        for i in range(ids.shape[0]):
            out[i, 0, 0] = float(self._seen + i)
            out[i, 0, 1] = 0.5
        self._seen += ids.shape[0]
        return [out]


def _bare_encoder() -> tuple[embedding.BgeEncoder, _FakeSession]:
    """绕过 __init__ 的 HF 缓存查找（CI 无模型缓存）：只注入 encode 的两个协作者。"""
    enc = embedding.BgeEncoder.__new__(embedding.BgeEncoder)
    enc._tokenizer, enc._session = _FakeTokenizer(), _FakeSession()
    return enc, enc._session


@pytest.mark.skipif(not embedding.VEC_AVAILABLE, reason="vec extra not installed")
class TestEncodeChunking:
    def test_large_input_split_into_bounded_batches(self) -> None:
        """70 条 ⇒ session.run 分 ceil(70/ENCODE_CHUNK) 次、每次 batch 有界：
        单次巨批 run 是大规模 rebuild 卡死 40+ 分钟的根因（issue #16）。"""
        enc, sess = _bare_encoder()
        enc.encode([f"doc {i}" for i in range(70)])
        assert len(sess.batch_sizes) == 3
        assert all(b <= embedding.ENCODE_CHUNK for b in sess.batch_sizes)
        assert sum(sess.batch_sizes) == 70

    def test_output_matches_input_order_and_unit_norm_across_chunks(self) -> None:
        """跨块拼接不丢行不错位：条数一致、每行 L2 范数为 1、全局行号严格递增。"""
        import numpy as np

        enc, _ = _bare_encoder()
        n = embedding.ENCODE_CHUNK + 5  # 强制跨块边界
        arr = np.array(enc.encode([f"doc {i}" for i in range(n)]), dtype=np.float32)
        assert len(arr) == n
        assert np.allclose(np.linalg.norm(arr, axis=1), 1.0, atol=1e-5)
        assert (np.diff(arr[:, 0]) > 0).all()  # 行号标记严格递增 = 顺序保持

    def test_single_doc_path_unchanged(self) -> None:
        """单条（写入路径常态）仍是一次 run、返回一行——热路径零回归。"""
        enc, sess = _bare_encoder()
        assert len(enc.encode(["single doc"])) == 1
        assert sess.batch_sizes == [1]
