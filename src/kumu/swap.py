"""交代できる人を探す。

「この日やっぱり入れなくなった」は、シフトを組んだあとで必ず起きる。
そのときに店長が電話をかけて回るのが普通だと思うが、誰に頼めば成立するかは
本当は計算で分かる。連勤の上限に当たる人、同じ日に別のコマへ入っている人、
その持ち場ができない人。頼んでも無理な相手を先に外せる。

やっていることは単純で、**その人をそのコマから外した状態でもう一度解く**。
解ければ、代わりにそこへ入った人が交代候補になる。
解けなければ、代われる人はいない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .model import SLOT_BY_KEY, Assignment, Request, Schedule, Shop, Wish
from .solver import Relaxable, ShiftSolver


@dataclass
class SwapResult:
    """交代を探した結果。"""

    possible: bool
    substitutes: list[str] = field(default_factory=list)  # 代わりに入る人の名前
    side_effects: list[str] = field(default_factory=list)  # 他に動く人
    blockers: list[Relaxable] = field(default_factory=list)  # 代われない理由
    cost_delta: int = 0
    undecided: bool = False  # 時間内に判断できなかった（代われないのとは別）


def find_substitute(
    shop: Shop,
    schedule: Schedule,
    staff_id: str,
    day: date,
    slot_key: str,
    *,
    time_limit_sec: float = 10.0,
) -> SwapResult:
    """その人がそのコマを外れたとき、代わりに入れる人がいるかを調べる。"""
    requests = [
        r
        for r in shop.requests
        if not (r.staff_id == staff_id and r.day == day and r.slot_key == slot_key)
    ]
    requests.append(
        Request(staff_id=staff_id, day=day, slot_key=slot_key, wish=Wish.IMPOSSIBLE)
    )

    trial = Shop(
        name=shop.name,
        start=shop.start,
        days=shop.days,
        staff=shop.staff,
        demands=shop.demands,
        requests=requests,
        rules=shop.rules,
    )
    result = ShiftSolver(trial, time_limit_sec=time_limit_sec).solve()

    if result.undecided:
        # 代われる人がいないのか、まだ分からないのかは別の話。
        # 一緒にすると、頼めば代われる人がいるのに諦めることになる
        return SwapResult(possible=False, undecided=True)

    if not result.feasible or result.schedule is None:
        return SwapResult(possible=False, blockers=result.conflicts)

    before = {(a.staff_id, a.day, a.slot_key) for a in schedule.assignments}
    after = {(a.staff_id, a.day, a.slot_key) for a in result.schedule.assignments}

    # 同じコマに新しく入った人が、直接の交代相手
    subs = sorted(
        shop.staff_by_id(sid).name
        for (sid, d, sk) in after - before
        if d == day and sk == slot_key
    )
    # それ以外の動きは、玉突きで動いた人
    others = sorted(
        {
            shop.staff_by_id(sid).name
            for (sid, d, sk) in (after - before) | (before - after)
            if not (d == day and sk == slot_key) and sid != staff_id
        }
    )

    return SwapResult(
        possible=True,
        substitutes=subs,
        side_effects=others,
        cost_delta=result.schedule.labor_cost - schedule.labor_cost,
    )


def schedule_from_report(shop: Shop, calendar: list[dict]) -> Schedule:
    """画面が持っている結果から Schedule を組み直す。

    交代を探すたびに全部を解き直したくないので、保存した結果を使えるようにする。
    """
    name_to_id = {s.name: s.id for s in shop.staff}
    assignments = []
    for day in calendar:
        d = date.fromisoformat(day["date"])
        for slot in day["slots"]:
            for p in slot["assigned"]:
                sid = name_to_id.get(p["name"])
                if not sid:
                    continue
                role = next(r for r in shop.staff_by_id(sid).roles if r.value == p["role"])
                assignments.append(
                    Assignment(staff_id=sid, day=d, slot_key=slot["key"], role=role)
                )
    cost = sum(
        shop.staff_by_id(a.staff_id).hourly_wage * SLOT_BY_KEY[a.slot_key].hours
        for a in assignments
    )
    return Schedule(assignments=assignments, labor_cost=cost)
