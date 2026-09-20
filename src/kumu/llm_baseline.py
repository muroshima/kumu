"""比較のために、言語モデルにシフトを組ませる。

kumu の主張は「言語モデルにシフトは作れない」。主張である以上、
作らせてみて何が起きるかを見せないと、ただの言い分になる。

やり方は既存実装（Gemini + LangChain で生成 → 自己評価 → 修正）に寄せてある。
モデルに表を出させ、モデル自身に採点させ、その指摘で直させる。
何周回しても、守れているかどうかは出してみるまで分からない。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .llm import LLM
from .model import SLOT_BY_KEY, SLOTS, Assignment, Role, Schedule, Shop, Wish
from .verify import verify

WEEKDAY = ("月", "火", "水", "木", "金", "土", "日")

SYSTEM_PROMPT = """あなたは飲食店の店長です。与えられた条件でシフトを組んでください。

守ること:
- 各コマの必要人数を満たす
- 「入れない」と出した人を、その日その時間に入れない
- 1人が1日に入るのは1コマまで
- 連続勤務は6日まで
- 遅番（17-23時）の翌日に早番（9-15時）を入れない（間隔が11時間必要）
- 週の労働時間は、人ごとの上限を超えない
- その人ができる持ち場にだけ入れる
- どのコマにも経験者を1人以上入れる

出力は次の JSON だけ。前置きも説明も付けない。

{"assignments":[{"staff_id":"S01","date":"2026-10-05","slot":"early","role":"ホール"}]}

slot は early / mid / late のいずれか。role はその人ができる持ち場から選ぶ。"""

REVIEW_PROMPT = """さきほどあなたが組んだシフトを、自分で採点してください。

100点から減点していきます。
- 必要人数を割っているコマ: 1つにつき -3
- 入れないと出した人を入れている: 1件につき -5
- 連勤・勤務間隔・週の上限を超えている: 1件につき -5
- できない持ち場に入れている: 1件につき -5

出力は次の JSON だけ。

{"score": <点数>, "problems": ["<問題点を1行ずつ>"]}"""


@dataclass
class BaselineResult:
    schedule: Schedule | None
    rounds: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    self_scores: list[int] = field(default_factory=list)
    parse_errors: int = 0
    truncated: bool = False  # 出力が上限で切れたか
    raw_last: str = ""


def _staff_table(shop: Shop) -> str:
    lines = []
    for s in shop.staff:
        roles = "・".join(r.value for r in s.roles)
        vet = "経験者" if s.is_veteran else "新人"
        lines.append(
            f"  {s.id} {s.name}／{roles}／{vet}／週{s.min_hours_per_week}〜{s.max_hours_per_week}時間"
        )
    return "\n".join(lines)


def _demand_table(shop: Shop) -> str:
    lines = []
    for day in shop.dates:
        parts = []
        for slot in SLOTS:
            d = shop.demand(day, slot.key)
            if not d:
                continue
            need = "・".join(f"{r.value}{n}" for r, n in d.required.items() if n)
            parts.append(f"{slot.label}({need or '0'})")
        lines.append(f"  {day:%Y-%m-%d}（{WEEKDAY[day.weekday()]}） {' '.join(parts)}")
    return "\n".join(lines)


def _request_table(shop: Shop) -> str:
    lines = []
    for r in shop.requests:
        if r.wish is Wish.OK:
            continue
        mark = {Wish.IMPOSSIBLE: "入れない", Wish.AVOID: "避けたい", Wish.WANT: "入りたい"}[r.wish]
        lines.append(
            f"  {r.staff_id} {r.day:%Y-%m-%d} {SLOT_BY_KEY[r.slot_key].label} {mark}"
        )
    return "\n".join(lines) if lines else "  （なし）"


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON が見つかりません")
    return json.loads(text[start : end + 1])


def _to_schedule(shop: Shop, payload: dict) -> Schedule:
    """モデルが返した表を、こちらのデータに直す。

    存在しない人や日付が混ざって返ることがあるので、ここでは落とさずに通す。
    どれだけ壊れているかも含めて数えたいので、検査は verify に任せる。
    """
    ids = {s.id for s in shop.staff}
    slots = {s.key for s in SLOTS}
    out = []
    for item in payload.get("assignments", []) or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("staff_id", ""))
        try:
            day = date.fromisoformat(str(item.get("date", "")))
        except ValueError:
            continue
        slot = str(item.get("slot", ""))
        role = None
        for r in Role:
            if r.value == item.get("role"):
                role = r
                break
        if role is None or sid not in ids or slot not in slots:
            # 壊れていても、壊れた事実を数えたいので落とさない
            role = role or Role.HALL
        out.append(Assignment(staff_id=sid, day=day, slot_key=slot, role=role))
    return Schedule(assignments=out)


def build_with_llm(
    shop: Shop, llm: LLM, *, model: str, rounds: int = 2, max_tokens: int = 20000
) -> BaselineResult:
    """モデルに組ませて、自己採点と修正を指定回数まわす。"""
    # 53コマぶんの JSON は長い。上限が足りないと途中で切れて、
    # 「モデルが悪い」のか「こちらの設定が悪い」のか分からなくなる
    result = BaselineResult(schedule=None)

    user = "\n".join(
        [
            "## スタッフ",
            _staff_table(shop),
            "",
            "## 各コマの必要人数",
            _demand_table(shop),
            "",
            "## 本人から出ている希望",
            _request_table(shop),
            "",
            "この条件でシフトを組んでください。JSON だけを返してください。",
        ]
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]

    for i in range(rounds + 1):
        try:
            res = llm.complete(messages, model=model, max_tokens=max_tokens)
        except Exception:  # noqa: BLE001 — 比較対象なので、落ちたらそこまで
            break

        result.calls += 1
        usage = res.get("usage") or {}
        result.prompt_tokens += int(usage.get("prompt_tokens", 0))
        result.completion_tokens += int(usage.get("completion_tokens", 0))
        result.raw_last = res["content"]

        try:
            payload = _extract_json(res["content"])
        except (ValueError, json.JSONDecodeError):
            result.parse_errors += 1
            # 上限に張り付いていたなら、出力が途中で切れたということ
            result.truncated = int(usage.get("completion_tokens", 0)) >= max_tokens - 50
            break

        result.schedule = _to_schedule(shop, payload)
        result.rounds = i + 1

        if i >= rounds:
            break

        # 自己採点 → その指摘で直させる（既存実装と同じ作り）
        messages.append({"role": "assistant", "content": res["content"]})
        messages.append({"role": "user", "content": REVIEW_PROMPT})
        try:
            review = llm.complete(messages, model=model, max_tokens=2000)
        except Exception:  # noqa: BLE001
            break
        result.calls += 1
        r_usage = review.get("usage") or {}
        result.prompt_tokens += int(r_usage.get("prompt_tokens", 0))
        result.completion_tokens += int(r_usage.get("completion_tokens", 0))
        try:
            scored = _extract_json(review["content"])
            result.self_scores.append(int(scored.get("score", 0)))
        except (ValueError, json.JSONDecodeError, TypeError):
            result.parse_errors += 1

        messages.append({"role": "assistant", "content": review["content"]})
        messages.append(
            {
                "role": "user",
                "content": "挙げた問題点を直したシフトを、もう一度 JSON で出してください。",
            }
        )

    return result


def check(shop: Shop, result: BaselineResult) -> dict[str, Any]:
    """組ませた結果が、守るべきことをどれだけ守れているか。"""
    if result.schedule is None:
        return {"ok": False, "reason": "シフトを取り出せなかった", "violations": None}
    v = verify(shop, result.schedule)
    return {
        "ok": v.ok,
        "violations": len(v.violations),
        "by_kind": v.by_kind(),
        "summary": v.summary(),
        "assignments": len(result.schedule.assignments),
    }
