"""BGE-small-zh 本地 embedding 编码器（向量召回路的模型缝）。

- 模型：Xenova/bge-small-zh-v1.5 ONNX int8（24MB，经 hf-mirror 下载到 HF 缓存，
  本地推理全程无外发，spec story 17）。缓存缺失时构造即失败——不自动联网下载。
- 依赖：属可选 extra `vec`（onnxruntime/tokenizers/numpy）。本模块 import 任何
  失败都置 VEC_AVAILABLE=False，由调用方降级为纯词面检索（检索降级不报错）。
- 编码语义（与 vec-spike 验证一致）：[CLS] 表示 + L2 归一化，512 维，
  truncation 512。单条长文档实测 ~400ms，查询短文本 ~15ms。
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Callable

EMBED_DIM = 512
_MODEL_GLOB = ".cache/huggingface/hub/models--Xenova--bge-small-zh-v1.5/snapshots/*"

try:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    VEC_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于安装环境是否带 vec extra
    VEC_AVAILABLE = False


class ModelMissingError(RuntimeError):
    """HF 缓存中找不到 BGE ONNX 模型（本地优先：不做自动下载，交调用方降级）。"""


def auto_encoder() -> "Callable[[list[str]], list[list[float]]] | None":
    """入口默认缝：依赖与模型都就绪才返回编码器（encode 绑定方法），否则 None（降级纯词面）。"""
    if not VEC_AVAILABLE:
        return None
    try:
        return BgeEncoder().encode
    except (ModelMissingError, OSError):
        return None


class BgeEncoder:
    """惰性加载的 ONNX 编码器；构造只解析模型路径，首次 encode 才建 session。"""

    def __init__(self, home: Path | None = None) -> None:
        if not VEC_AVAILABLE:
            raise ModelMissingError("vec dependencies not installed (pip install 'compound-memory[vec]')")
        base = home or Path.home()
        snaps = sorted(glob.glob(str(base / _MODEL_GLOB)))
        if not snaps:
            raise ModelMissingError(
                "BGE-small-zh ONNX model not found in HF cache; "
                "download via hf-mirror.com (see docs/specs/0001-compound-memory-spec.md)"
            )
        snap = Path(snaps[-1])
        self._onnx_path = snap / "onnx" / "model_quantized.onnx"
        self._tokenizer_path = snap / "tokenizer.json"
        if not self._onnx_path.exists() or not self._tokenizer_path.exists():
            raise ModelMissingError(f"incomplete model snapshot: {snap}")
        self._session: "ort.InferenceSession | None" = None
        self._tokenizer: "Tokenizer | None" = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        """批量编码；L2 归一化后的 [CLS] 表示（余弦可直接用作相似度）。"""
        assert VEC_AVAILABLE  # 构造已保证；reassure 类型检查
        if self._session is None or self._tokenizer is None:
            self._tokenizer = Tokenizer.from_file(str(self._tokenizer_path))
            self._tokenizer.enable_truncation(max_length=512)
            self._tokenizer.enable_padding()
            self._session = ort.InferenceSession(str(self._onnx_path), providers=["CPUExecutionProvider"])
        encs = self._tokenizer.encode_batch(texts)
        feed = {
            "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64),
        }
        names = {i.name for i in self._session.get_inputs()}
        out = self._session.run(None, {k: v for k, v in feed.items() if k in names})[0]
        cls = out[:, 0, :]
        normed = cls / np.linalg.norm(cls, axis=1, keepdims=True)
        return normed.tolist()
