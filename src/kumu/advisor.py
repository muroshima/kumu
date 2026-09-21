"""組めなかったとき、どの条件をゆずるかを AI に決めてもらう。

固定の優先順位で決めると、どの週でも同じ順に外すことになる。
現場ではそうはならない。「今週は土曜のベテランが三人とも私用で、
代わりが効かない」といった事情で、痛みの少ない譲り方は毎回違う。
そこを読むのが AI の仕事になる。

**選べる範囲はこちらが決める。** 渡すのは、緩めてよい候補だけ。
連勤や勤務間インターバルのような法令由来のものは、そもそも候補に
入っていない（solver.py で仮定リテラルを付けていない）。AI が何を
返しても、外してはいけないものは外れない。

**AI の言い分を信じない。** 返ってきた候補は、実際に外して解き直して
確かめる。「これを外せば組める」が事実かどうかは計算が決める。
AI が担当するのは「どれから試すか」の順番と、その理由の言語化まで。

**それでも確定はしない。** 外して組めた形は提案どまりで、外すかどうかを
決めるのは店長のまま。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .llm import BudgetExceeded
from .model import Shop
from .solver import Relaxable

SYSTEM_PROMPT = """あなたは飲食店の店長を手伝う担当です。

今週のシフトが、出された希望と店の決まりのままでは組めませんでした。
どれか一つをゆずれば組めるようになります。**どれからゆずるのが一番痛くないか**を
選んでください。

選ぶときの考え方:
- 店が自分で決められることを先に。人に頼み直すのは後
- 影響を受ける人が少ないものを先に
- 本人と交わした約束（契約の最低時間）を変えるのは最後
- 同じ日に複数の候補があるなら、その日の事情を見て選ぶ

出力は次の JSON だけ。前置きも説明も付けません。

{"key": "<候補のキーをそのまま>", "reason": "<なぜそれを選んだか。日本語で1〜2文>"}

key は必ず、渡された候補の中から選んでください。候補にないものを書いてはいけません。"""


@dataclass
class Advice:
    """AI が選んだ一手。まだ確かめていない。"""

    key: str
    reason: str
    raw: str = ""
    error: str | None = None


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON が見つかりません")
    return json.loads(text[start : end + 1])


def _extra(shop: Shop, r: Relaxable) -> str:
    """その候補をゆずると何が起きるか、分かる範囲で添える。

    候補の名前だけを並べても、どれが痛いかは決まらない。
    「その日に入れないと言っている本人が、何を書いていたか」まで渡して
    初めて、その週の事情で選べるようになる。
    """
    kind, *rest = r.key.split(":")

    if kind == "ng" and len(rest) >= 3:
        staff_id, day_iso = rest[0], rest[1]
        try:
            day = date.fromisoformat(day_iso)
        except ValueError:
            return ""
        notes = {
            q.note.strip()
            for q in shop.requests
            if q.staff_id == staff_id and q.day == day and q.note.strip()
            and q.note != "画面から選択"
        }
        who = shop.staff_by_id(staff_id)
        bits = [f"本人が書いた事情: {' / '.join(notes)}" if notes else "本人は理由を書いていない"]
        bits.append(f"できる持ち場: {'・'.join(x.value for x in who.roles)}")
        bits.append("経験者" if who.is_veteran else "新人")
        return "ゆずると、この人に予定を変えてもらう話になる。" + " ／ ".join(bits)

    if kind == "demand" and len(rest) >= 3:
        return "ゆずると、そのコマを1人少ない人数で回すことになる。"

    if kind == "veteran":
        return "ゆずると、そのコマに経験者がいない状態で回すことになる。"

    if kind == "minhours" and rest:
        who = shop.staff_by_id(rest[0]) if rest[0] else None
        if who:
            return (
                f"ゆずると、{who.name}さんに約束した週{who.min_hours_per_week}時間を"
                "渡せなくなる。本人との契約の話になる。"
            )
    if kind == "cost":
        return "ゆずると、今週の人件費が予算を超える。"
    return ""


def _context(shop: Shop, candidates: list[Relaxable]) -> str:
    vets = [s.name for s in shop.staff if s.is_veteran]
    lines = [
        "## 店の状況",
        f"  スタッフ {len(shop.staff)}人（うち経験者 {len(vets)}人）",
        f"  対象の週 {shop.start:%Y-%m-%d} から {shop.days}日",
        "",
        "## ゆずれる候補",
    ]
    for r in candidates:
        lines.append(f"  key={r.key}")
        lines.append(f"    {r.label}")
        extra = _extra(shop, r)
        if extra:
            lines.append(f"    {extra}")
    lines += [
        "",
        "この中から一つ選んで、JSON で返してください。",
        "理由には、その週の事情（誰が何を書いていたか、どのコマが薄いか）を入れてください。",
        "一般論だけの理由は避けてください。",
    ]
    return "\n".join(lines)


class Advisor:
    """どの条件からゆずるかを AI に選ばせる。"""

    def __init__(self, llm, *, model: str | None = None) -> None:
        self.llm = llm
        self.model = model

    def choose(self, shop: Shop, candidates: list[Relaxable]) -> Advice | None:
        """候補から一つ選ぶ。選べなかったら None（呼び出し側が既定の順に戻す）。"""
        if not candidates:
            return None

        kwargs: dict[str, Any] = {"max_tokens": 400}
        if self.model:
            kwargs["model"] = self.model

        try:
            res = self.llm.complete(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _context(shop, candidates)},
                ],
                **kwargs,
            )
            parsed = _extract_json(res["content"])
        except BudgetExceeded:
            # 上限は止めるために置いてある。ここで飲み込むと止まらない
            raise
        except Exception as e:  # noqa: BLE001 — 選べなければ既定の順で進める
            return Advice(key="", reason="", error=f"{type(e).__name__}: {e}")

        key = str(parsed.get("key", ""))
        valid = {r.key for r in candidates}
        if key not in valid:
            # 候補にないものを返してきた。従わない
            return Advice(
                key="",
                reason="",
                raw=res["content"][:200],
                error=f"候補にないキーを返した: {key[:60]!r}",
            )

        return Advice(key=key, reason=str(parsed.get("reason", ""))[:200], raw=res["content"][:200])
