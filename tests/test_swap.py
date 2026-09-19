"""交代相手を探す部分のテスト。

「加藤さんなら代われます」と画面に出す以上、その人が本当に入れることを
確かめておかないと、頼んでから断られる。人に頼む前に機械で確かめるのが
この機能の目的なので、そこが崩れると存在理由が無くなる。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kumu.model import (  # noqa: E402
    SLOT_BY_KEY,
    SLOTS,
    Demand,
    Request,
    Role,
    Rules,
    Shop,
    Staff,
    Wish,
)
from kumu.solver import ShiftSolver  # noqa: E402
from kumu.swap import find_substitute  # noqa: E402

START = date(2026, 10, 5)


def shop_with(
    staff: list[Staff], *, days: int = 3, requests=None, required=None, kitchen_only_mid=False
) -> Shop:
    """テスト用の店。

    1人が1日に入れるのは1コマなので、必要コマ数に対して人数が足りないと
    そもそも組めない。前提が組めていないとテストの意味が無くなる。
    """
    required = required or {Role.HALL: 1, Role.KITCHEN: 1}
    demands = []
    for i in range(days):
        for slot in SLOTS:
            need = dict(required)
            if kitchen_only_mid and slot.key != "mid":
                need.pop(Role.KITCHEN, None)
            demands.append(Demand(START + timedelta(days=i), slot.key, need))
    return Shop(
        name="テスト店",
        start=START,
        days=days,
        staff=staff,
        demands=demands,
        requests=requests or [],
        rules=Rules(veteran_required_per_slot=False),
    )


def roomy_staff() -> list[Staff]:
    return [
        Staff("A", "あさひ", [Role.HALL, Role.KITCHEN], 1200, True, 40, 0),
        Staff("B", "ばん", [Role.HALL, Role.KITCHEN], 1100, True, 40, 0),
        Staff("C", "ちひろ", [Role.HALL], 1000, False, 40, 0),
        Staff("D", "だいち", [Role.KITCHEN], 1000, False, 40, 0),
        Staff("E", "えいじ", [Role.HALL, Role.KITCHEN], 1000, False, 40, 0),
        Staff("F", "ふうか", [Role.HALL], 1000, False, 40, 0),
        Staff("G", "げん", [Role.KITCHEN], 1000, False, 40, 0),
        Staff("H", "はるか", [Role.HALL, Role.KITCHEN], 1000, False, 40, 0),
        Staff("I", "いつき", [Role.HALL], 1000, False, 40, 0),
        Staff("J", "じゅん", [Role.KITCHEN], 1000, False, 40, 0),
    ]


def solve(shop: Shop):
    res = ShiftSolver(shop, time_limit_sec=25).solve()
    assert res.feasible, "前提のシフトが組めていない"
    return res.schedule


class Test交代できるとき:
    def test_挙げた候補は本当にそのコマに入れる(self):
        shop = shop_with(roomy_staff())
        schedule = solve(shop)
        target = schedule.assignments[0]

        res = find_substitute(
            shop, schedule, target.staff_id, target.day, target.slot_key
        )

        assert res.possible
        # 候補として挙げた人が、その持ち場をこなせること
        names = {s.name: s for s in shop.staff}
        for name in res.substitutes:
            sub = names[name]
            assert any(sub.can(r) for r in (target.role, Role.HALL, Role.KITCHEN))

    def test_交代しても制約は崩れない(self):
        """代わりを入れた結果が、元と同じだけ制約を満たしていること。"""
        shop = shop_with(roomy_staff())
        schedule = solve(shop)
        target = schedule.assignments[0]

        # 本人を外した状態で組み直したものが、制約を満たすかを直接確かめる
        requests = [
            Request(target.staff_id, target.day, target.slot_key, Wish.IMPOSSIBLE)
        ]
        after = solve(shop_with(roomy_staff(), requests=requests))

        for demand in shop.demands:
            for role, need in demand.required.items():
                got = sum(
                    1
                    for a in after.assignments
                    if a.day == demand.day and a.slot_key == demand.slot_key and a.role == role
                )
                assert got >= need

    def test_本人はそのコマから外れる(self):
        shop = shop_with(roomy_staff())
        schedule = solve(shop)
        target = schedule.assignments[0]

        requests = [
            Request(target.staff_id, target.day, target.slot_key, Wish.IMPOSSIBLE)
        ]
        after = solve(shop_with(roomy_staff(), requests=requests))

        assert not [
            a
            for a in after.assignments
            if a.staff_id == target.staff_id
            and a.day == target.day
            and a.slot_key == target.slot_key
        ]


class Test交代できないとき:
    def test_代われる人がいなければ理由を返す(self):
        """キッチンに入れるのが1人しかいない状況を作る。"""
        staff = [
            Staff("A", "あさひ", [Role.HALL], 1200, True, 40, 0),
            Staff("B", "ばん", [Role.HALL], 1100, True, 40, 0),
            Staff("C", "ちひろ", [Role.HALL], 1000, False, 40, 0),
            Staff("K", "きっちん", [Role.KITCHEN], 1000, True, 40, 0),
        ]
        shop = shop_with(staff, days=2, kitchen_only_mid=True)
        schedule = solve(shop)
        kitchen = next(a for a in schedule.assignments if a.role is Role.KITCHEN)

        res = find_substitute(shop, schedule, "K", kitchen.day, kitchen.slot_key)

        assert not res.possible, "代われる人がいないのに代われると答えている"
        assert res.blockers, "代われない理由を挙げられていない"

    def test_挙げる理由はその日のことに限る(self):
        staff = [
            Staff("A", "あさひ", [Role.HALL], 1200, True, 40, 0),
            Staff("B", "ばん", [Role.HALL], 1100, True, 40, 0),
            Staff("C", "ちひろ", [Role.HALL], 1000, False, 40, 0),
            Staff("K", "きっちん", [Role.KITCHEN], 1000, True, 40, 0),
        ]
        shop = shop_with(staff, days=2, kitchen_only_mid=True)
        schedule = solve(shop)
        kitchen = next(a for a in schedule.assignments if a.role is Role.KITCHEN)

        res = find_substitute(shop, schedule, "K", kitchen.day, kitchen.slot_key)

        # 無関係な日の制約を原因として並べない
        for c in res.blockers:
            assert kitchen.day.isoformat() in c.key or c.key.startswith("cost"), (
                f"関係のない制約を挙げている: {c.label}"
            )
