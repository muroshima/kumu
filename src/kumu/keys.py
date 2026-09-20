"""確認済みかどうかを覚えておくための識別子。

同じ希望が組み直すたびに出し直されるので、内容から作る。連番を振ると
組み直しのたびに別物になり、店長が確認したことが効かなくなる。

**組み込みの `hash()` は使えない。** 文字列のハッシュはプロセスごとに
変わるので、画面のプロセスで作った識別子と、組み立てのプロセスで作った
識別子が一致しない。承認したはずのものが毎回確認待ちに戻ってくる。
"""

from __future__ import annotations

import hashlib


def ack_key(*parts: object) -> str:
    """内容から決まる識別子。プロセスをまたいでも同じ値になる。"""
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def proposal_key(staff_name: str, note: str) -> str:
    """自由文の読み取り結果1件に対する識別子。

    画面（確認待ちのカード）と組み立て（反映するかどうかの判定）で
    同じものを使う。片方だけ変えると、承認が突き合わなくなる。
    """
    return ack_key("pending", staff_name, note)
