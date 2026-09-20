"""確信度の値を読む。

`nan` は比較が全部 False になるので、`confidence < 0.6` の判定を素通りする。
保存ファイルにもモデルの返事にも入りうるので、**読むところで潰す**。
どちらか片方だけ直しても、もう片方から入ってくる。
"""

from __future__ import annotations

import math


def read_confidence(raw: object) -> float:
    """0.0〜1.0 の有限な値だけを通す。読めないものは 0.0（＝人に回る）。"""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))
