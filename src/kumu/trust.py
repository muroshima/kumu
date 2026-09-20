"""信頼ポイント。実績に応じて希望の通りやすさが変わる。

## なぜ要るか

シフトを守る人と、当日に落とす人が同じ重さで扱われると、守っている側が損をする。
「あの人はいつも急に休むのに、希望は全部通っている」は現場で一番もめる。

## なぜソルバーでやるか

店長が印象で優先順位を付けると、その判断の根拠を本人に説明できない。
点の増減を記録として残し、それを重みとして機械的に掛ければ、
「なぜ自分の希望が通らなかったか」に数字で答えられる。

## やりすぎないための線

重みは 0.6〜1.3 倍の範囲に収めてある。一度遅刻しただけで永久に
シフトに入れなくなると、それは罰であって調整ではない。
**下限（契約の最低時間）には一切影響させない。** 信頼が低くても、
約束した時間は割り当てる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# 何が起きたら何点動くか。減点は小さく、回復はゆっくり。
EVENTS: dict[str, tuple[int, str]] = {
    "no_show": (-15, "無断欠勤"),
    "late": (-5, "遅刻"),
    "late_cancel": (-8, "前日以降の取り消し"),
    "swap_request": (-2, "交代を依頼した"),
    "swap_accepted": (+4, "交代を引き受けた"),
    "worked": (+1, "予定どおり勤務した"),
    "manual": (0, "手動調整"),
}

MIN_TRUST, MAX_TRUST, DEFAULT_TRUST = 40, 100, 100


@dataclass
class TrustEvent:
    staff_id: str
    kind: str
    delta: int
    at: str
    note: str = ""


def load_events(path: Path) -> list[TrustEvent]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        kind = str(d.get("kind", ""))
        if kind not in EVENTS:
            continue  # 知らないできごとは点を動かさない
        # 記録された delta は使わない。no_show の行の delta を +100 に
        # 書き換えるだけで、減点をなかったことにできてしまう。
        # 何が起きたか（kind）だけを記録として受け取り、重みは EVENTS で決める
        delta, label = EVENTS[kind]
        out.append(
            TrustEvent(
                staff_id=str(d.get("staff_id", "")),
                kind=kind,
                delta=delta,
                at=str(d.get("at", "")),
                note=str(d.get("note", "") or label),
            )
        )
    return out


def append_event(path: Path, staff_id: str, kind: str, note: str = "") -> TrustEvent:
    delta, label = EVENTS.get(kind, (0, kind))
    ev = TrustEvent(
        staff_id=staff_id,
        kind=kind,
        delta=delta,
        at=datetime.now().isoformat(timespec="seconds"),
        note=note or label,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev.__dict__, ensure_ascii=False) + "\n")
    return ev


def scores(events: list[TrustEvent]) -> dict[str, int]:
    """いまの点数。"""
    out: dict[str, int] = {}
    for ev in events:
        cur = out.get(ev.staff_id, DEFAULT_TRUST)
        out[ev.staff_id] = max(MIN_TRUST, min(MAX_TRUST, cur + ev.delta))
    return out


def history_of(events: list[TrustEvent], staff_id: str, limit: int = 10) -> list[TrustEvent]:
    return [e for e in events if e.staff_id == staff_id][-limit:][::-1]
