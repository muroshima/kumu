"""画面からの入力を取り込んだ状態の店を組み立てる。

シフトを組むときと、あとから交代相手を探すときとで、**同じ店を見ていないと
話が合わない**。組んだあとに出された希望や、店長が確認した内容、信頼ポイントの
増減が入っていない店で解き直すと、元のシフトと違う制約で候補を出すことになる。

「代われます」と言われて頼んだのに、実際には連勤に当たっていた、
というのが一番まずい壊れ方なので、組み立てをここ1か所に寄せてある。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .dummy import build
from .inbox import apply_submissions, apply_trust
from .model import Shop


def load_decisions(path: Path) -> dict:
    """店長が確認待ちに対して決めたこと。壊れていたら空として扱う。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def shop_for_week(
    runs_dir: Path,
    *,
    start: date,
    days: int,
    impossible_week: bool = False,
    use_inbox: bool = True,
) -> Shop:
    """その週の店を、画面からの入力込みで組み立てる。"""
    shop = build(days=days, start=start, impossible_week=impossible_week)
    if not use_inbox:
        return shop

    decisions = load_decisions(runs_dir / "decisions.json")
    apply_submissions(shop, runs_dir / "submissions.jsonl", decisions=decisions)
    apply_trust(shop, runs_dir / "trust.jsonl")
    return shop
