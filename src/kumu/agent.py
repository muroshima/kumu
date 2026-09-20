"""組めなかったときに、自分で手を打つ。

一度解いて終わりなら、それはただの計算で、エージェントとは呼べない。
現場の店長は、組めなかったらそこで止まらない。必要人数を見直したり、
誰かに相談したり、ルールを一時的に外したりして、組める形を探す。

ここではそれを回す。

    解く
      組めた   → 終わり
      組めない → 矛盾している制約を受け取る
                 ↓
                 緩められる候補を出す（法令は候補に出さない）
                 ↓
                 動かしたときの影響が小さい順に、実際に試す
                 ↓
                 組めたら、何を動かしたかを添えて人に返す
                 全部だめなら、試した記録を添えて人に返す

**勝手に確定はしない。** ルールを外して組めたシフトは、店長が外すと
決めない限り提案どまりにする。緩めてよいかを決めるのは人の側で、
どれを緩めれば組めるかを調べるのが機械の側、という分け方にしてある。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .explain import group_conflicts
from .model import Role, Rules, Shop, Wish
from .solver import Relaxable, ShiftSolver, SolveResult

# 動かしやすい順。人に頼むのが一番あとに来る。
# 店が自分で決められることから試す
PRIORITY = {"veteran": 0, "cost": 1, "demand": 2, "ng": 3}


@dataclass
class Step:
    """1回の試行。"""

    action: str  # 何をしたか
    detail: str
    feasible: bool
    conflicts: int = 0
    seconds: float = 0.0


@dataclass
class AgentResult:
    result: SolveResult
    steps: list[Step] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)  # 組むために動かしたもの
    proposal: bool = False  # ルールを動かして組めた＝提案どまり

    @property
    def feasible(self) -> bool:
        return self.result.feasible


def _relax_label(r: Relaxable) -> str:
    kind = r.key.split(":")[0]
    if kind == "veteran":
        return f"{r.label} をやめる"
    if kind == "demand":
        return f"{r.label} を1人減らす"
    if kind == "cost":
        return "人件費の上限を外す"
    if kind == "ng":
        return f"{r.label} を外す（本人への確認が要る）"
    return r.label


def _apply(shop: Shop, r: Relaxable) -> Shop:
    """その制約を外した店を作る。元の店は触らない。"""
    kind, *rest = r.key.split(":")

    demands = [
        type(d)(day=d.day, slot_key=d.slot_key, required=dict(d.required))
        for d in shop.demands
    ]
    requests = list(shop.requests)
    rules = Rules(**{**shop.rules.__dict__})

    if kind == "veteran":
        # そのコマだけ外すことはできないので、店のルールごと落とす。
        # 影響が広いぶん、提案として人に見せる前提にしてある
        rules.veteran_required_per_slot = False
    elif kind == "cost":
        rules.labor_cost_limit_per_week = None
    elif kind == "demand" and len(rest) >= 3:
        day = date.fromisoformat(rest[0])
        slot_key, role_name = rest[1], rest[2]
        for d in demands:
            if d.day == day and d.slot_key == slot_key:
                for role in Role:
                    if role.value == role_name and d.required.get(role, 0) > 0:
                        d.required[role] -= 1
    elif kind == "ng" and len(rest) >= 3:
        staff_id, day_iso, slot_key = rest[0], rest[1], rest[2]
        day = date.fromisoformat(day_iso)
        requests = [
            q
            for q in requests
            if not (
                q.staff_id == staff_id
                and q.day == day
                and q.slot_key == slot_key
                and q.wish is Wish.IMPOSSIBLE
            )
        ]

    return Shop(
        name=shop.name,
        start=shop.start,
        days=shop.days,
        staff=shop.staff,
        demands=demands,
        requests=requests,
        rules=rules,
        load_preferences=list(shop.load_preferences),
    )


def run(
    shop: Shop,
    *,
    time_limit_sec: float = 20.0,
    max_attempts: int = 6,
    on_step=None,
) -> AgentResult:
    """組めるまで手を打つ。打った手は全部記録に残す。"""
    first = ShiftSolver(shop, time_limit_sec=time_limit_sec).solve()
    steps = [
        Step(
            action="そのまま解く",
            detail="出された希望と店のルールをすべて満たす形を探す",
            feasible=first.feasible,
            conflicts=len(first.conflicts),
            seconds=first.wall_time_sec,
        )
    ]
    if on_step:
        on_step(steps[-1])

    if first.feasible:
        return AgentResult(result=first, steps=steps)

    # 時間切れのときに緩め始めるのが一番まずい。何が悪いのか分かっていないのに
    # 条件を外すことになる。組めない証拠がないうちは、手を打たずに人に返す
    if first.undecided:
        steps[-1] = Step(
            action="そのまま解く",
            detail="時間内に組めるかどうかを判断できなかった",
            feasible=False,
            conflicts=0,
            seconds=first.wall_time_sec,
        )
        return AgentResult(result=first, steps=steps)

    # 組めなかった。1つ外すたびに矛盾の中身は変わるので、
    # そのつど取り直して次の手を決める。最初のリストを使い続けると、
    # 外したあとに初めて出てくる問題（必要人数が足りない等）に手が届かない
    current = shop
    res = first
    applied: list[str] = []
    seen_once: set[str] = set()

    for _ in range(max_attempts):
        if res.feasible:
            break
        candidates = sorted(
            res.conflicts, key=lambda r: PRIORITY.get(r.key.split(":")[0], 9)
        )
        # 同じ種類を繰り返しても代わり映えしないものは1回だけ
        pick = None
        for cand in candidates:
            kind = cand.key.split(":")[0]
            if kind in ("veteran", "cost") and kind in seen_once:
                continue
            pick = cand
            if kind in ("veteran", "cost"):
                seen_once.add(kind)
            break
        if pick is None:
            break

        current = _apply(current, pick)
        res = ShiftSolver(current, time_limit_sec=time_limit_sec).solve()
        applied.append(_relax_label(pick))
        step = Step(
            action=_relax_label(pick),
            detail="この条件を外して組み直した",
            feasible=res.feasible,
            conflicts=len(res.conflicts),
            seconds=res.wall_time_sec,
        )
        steps.append(step)
        if on_step:
            on_step(step)

    if res.feasible:
        return AgentResult(result=res, steps=steps, applied=applied, proposal=True)
    return AgentResult(result=first, steps=steps, applied=applied)


def describe(shop: Shop, agent: AgentResult) -> str:
    """やったことを人に読める形にする。"""
    lines = ["エージェントがやったこと:"]
    for i, s in enumerate(agent.steps, 1):
        if s.feasible:
            mark = "組めた"
        elif "判断できなかった" in s.detail:
            mark = "時間内に判断できず"
        else:
            mark = f"組めない（矛盾 {s.conflicts}件）"
        lines.append(f"  {i}. {s.action} → {mark}  [{s.seconds:.2f}秒]")

    if agent.feasible and agent.proposal:
        lines += [
            "",
            "そのままでは組めなかったので、次を外せば組めることを確かめました。",
        ]
        lines += [f"  - {a}" for a in agent.applied]
        lines += ["", "外してよいかは店長が決めてください。これは提案で、確定ではありません。"]
    elif agent.result.undecided:
        lines += [
            "",
            "時間内に判断できませんでした。**組めないと分かったわけではありません。**",
            "制限時間を延ばすか、対象の週を短くして試してください。",
        ]
    elif not agent.feasible:
        lines += ["", "試した範囲では組めませんでした。同時に成り立たない条件:"]
        lines += [f"  - {x}" for x in group_conflicts(agent.result.conflicts)]

    return "\n".join(lines)
