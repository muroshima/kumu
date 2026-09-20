"""解が制約を守っているかのテスト。

このプログラムの主張は「返ってきたシフトは制約を必ず満たしている」で、
それが本当かをここで確かめる。守れていないなら主張ごと嘘になる。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kumu.dummy import build  # noqa: E402
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
from kumu.verify import verify  # noqa: E402


def small_shop(**over) -> Shop:
    """テスト用の小さい店。余裕を持たせてあるので普通は解ける。"""
    start = over.pop("start", date(2026, 10, 1))
    days = over.pop("days", 3)
    # 1人が1日に入れるのは1コマなので、必要コマ数に対して人数が足りないと解けない。
    # ここは制約が守られているかを見たいので、余裕を持たせておく。
    staff = over.pop(
        "staff",
        [
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
        ],
    )
    demands = over.pop(
        "demands",
        [
            Demand(start + timedelta(days=i), slot.key, {Role.HALL: 1, Role.KITCHEN: 1})
            for i in range(days)
            for slot in SLOTS
        ],
    )
    return Shop(
        name="テスト店",
        start=start,
        days=days,
        staff=staff,
        demands=demands,
        requests=over.pop("requests", []),
        rules=over.pop("rules", Rules(veteran_required_per_slot=False)),
    )


class Test返ってきた解は制約を満たす:
    def test_必要人数を満たしている(self):
        shop = small_shop()
        res = ShiftSolver(shop, time_limit_sec=20).solve()
        assert res.feasible

        for demand in shop.demands:
            for role, need in demand.required.items():
                got = sum(
                    1
                    for a in res.schedule.assignments
                    if a.day == demand.day and a.slot_key == demand.slot_key and a.role == role
                )
                assert got >= need, f"{demand.day} {demand.slot_key} {role.value}: {got} < {need}"

    def test_不可と出した日には入っていない(self):
        day = date(2026, 10, 2)
        reqs = [
            Request("A", day, slot.key, Wish.IMPOSSIBLE) for slot in SLOTS
        ]
        shop = small_shop(requests=reqs)
        res = ShiftSolver(shop, time_limit_sec=20).solve()
        assert res.feasible

        assert not [a for a in res.schedule.assignments if a.staff_id == "A" and a.day == day]

    def test_1日に2コマ入らない(self):
        shop = small_shop()
        res = ShiftSolver(shop, time_limit_sec=20).solve()
        seen: dict[tuple[str, date], int] = {}
        for a in res.schedule.assignments:
            seen[(a.staff_id, a.day)] = seen.get((a.staff_id, a.day), 0) + 1
        assert max(seen.values()) <= 1

    def test_連勤の上限を超えない(self):
        shop = small_shop(days=10, rules=Rules(max_days_in_a_row=3, veteran_required_per_slot=False))
        res = ShiftSolver(shop, time_limit_sec=25).solve()
        assert res.feasible

        for s in shop.staff:
            worked = sorted({a.day for a in res.schedule.assignments if a.staff_id == s.id})
            run = longest = 0
            prev = None
            for d in worked:
                run = run + 1 if prev and (d - prev).days == 1 else 1
                longest = max(longest, run)
                prev = d
            assert longest <= 3, f"{s.name} が {longest} 日連続"

    def test_遅番の翌日に早番が入らない(self):
        """勤務間インターバル。遅番は23時終わり、早番は9時始まりで10時間しか空かない。"""
        shop = small_shop(days=6)
        res = ShiftSolver(shop, time_limit_sec=20).solve()
        assert res.feasible

        by_staff_day = {(a.staff_id, a.day): a.slot_key for a in res.schedule.assignments}
        for (sid, day), slot in by_staff_day.items():
            if slot != "late":
                continue
            assert by_staff_day.get((sid, day + timedelta(days=1))) != "early"

    def test_週の上限時間を超えない(self):
        shop = small_shop(days=7)
        res = ShiftSolver(shop, time_limit_sec=25).solve()
        assert res.feasible

        for s in shop.staff:
            hours = sum(
                SLOT_BY_KEY[a.slot_key].hours
                for a in res.schedule.assignments
                if a.staff_id == s.id
            )
            assert hours <= s.max_hours_per_week

    def test_できない持ち場に入らない(self):
        shop = small_shop()
        res = ShiftSolver(shop, time_limit_sec=20).solve()
        for a in res.schedule.assignments:
            assert shop.staff_by_id(a.staff_id).can(a.role)


class Test満たせないときは解を返さない:
    def test_人が足りなければ組まない(self):
        """1人しかいないのに全コマ2人必要。何かを出すのではなく、出さないのが正しい。"""
        start = date(2026, 10, 1)
        shop = small_shop(
            days=3,
            staff=[Staff("A", "あさひ", [Role.HALL, Role.KITCHEN], 1200, True, 40, 0)],
            demands=[
                Demand(start + timedelta(days=i), slot.key, {Role.HALL: 1, Role.KITCHEN: 1})
                for i in range(3)
                for slot in SLOTS
            ],
        )
        res = ShiftSolver(shop, time_limit_sec=15).solve()

        assert not res.feasible
        assert res.schedule is None  # 中途半端なシフトを返さない

    def test_矛盾している制約を特定できる(self):
        """全員が同じ日に入れないと出したら、その日の必要人数と両立しない。"""
        day = date(2026, 10, 2)
        reqs = [
            Request(s, day, slot.key, Wish.IMPOSSIBLE)
            for s in "ABCDEFGHIJ"
            for slot in SLOTS
        ]
        shop = small_shop(days=3, requests=reqs)
        res = ShiftSolver(shop, time_limit_sec=15).solve()

        assert not res.feasible
        assert res.conflicts, "原因を1つも挙げられていない"

        keys = {c.key.split(":")[0] for c in res.conflicts}
        assert keys <= {"ng", "demand", "veteran", "cost"}
        # 関係ない日の制約を原因として挙げていないこと
        for c in res.conflicts:
            assert day.isoformat() in c.key, f"無関係な制約を挙げている: {c.label}"

    def test_矛盾集合が絞り込まれている(self):
        """CP-SAT が返す集合は最小ではない。絞り込めていないと読めない量になる。"""
        shop = build(impossible_week=True, with_injection=False)
        res = ShiftSolver(shop, time_limit_sec=20).solve()

        assert not res.feasible
        assert len(res.conflicts) <= 20, f"{len(res.conflicts)} 件では多すぎて読めない"


class Test日付の付かない意思表示:
    def test_控えめにと言った人の時間が減る(self):
        from kumu.model import LoadPreference

        base = small_shop(days=5)
        got_base = ShiftSolver(base, time_limit_sec=20).solve()
        assert got_base.feasible
        hours_base = sum(
            SLOT_BY_KEY[a.slot_key].hours
            for a in got_base.schedule.assignments
            if a.staff_id == "A"
        )

        lighter = small_shop(days=5)
        lighter.load_preferences.append(
            LoadPreference(staff_id="A", level="lighter", reason="掛け持ちのため")
        )
        got = ShiftSolver(lighter, time_limit_sec=20).solve()
        assert got.feasible
        hours = sum(
            SLOT_BY_KEY[a.slot_key].hours
            for a in got.schedule.assignments
            if a.staff_id == "A"
        )

        assert hours <= hours_base, f"控えめにと言ったのに {hours_base}h → {hours}h"

    def test_控えめでも必要人数は割らない(self):
        """意思表示は目的関数側。守らなければならないことを崩さない。"""
        from kumu.model import LoadPreference

        shop = small_shop(days=5)
        for s in shop.staff:
            shop.load_preferences.append(
                LoadPreference(staff_id=s.id, level="lighter", reason="全員が控えめ希望")
            )
        res = ShiftSolver(shop, time_limit_sec=20).solve()

        assert res.feasible
        for demand in shop.demands:
            for role, need in demand.required.items():
                got = sum(
                    1
                    for a in res.schedule.assignments
                    if a.day == demand.day and a.slot_key == demand.slot_key and a.role == role
                )
                assert got >= need


class Test法令と店のルールを分ける:
    def test_法令由来の制約は緩める候補に出さない(self):
        """連勤や休憩を「緩めれば解けます」と提案してはいけない。"""
        shop = build(impossible_week=True, with_injection=False)
        res = ShiftSolver(shop, time_limit_sec=20).solve()

        for c in res.conflicts:
            kind = c.key.split(":")[0]
            assert kind not in ("days_in_a_row", "rest", "max_hours")


class Test契約の最低時間は必ず守る:
    """検査で違反として数えるものは、ソルバー側でも必ず守る側に置く。

    片方だけ厳しいと、「解けた」と言いながら検査を通らないシフトが出る。
    「満たせないなら返さない」が成り立たなくなる。
    """

    def test_解けたシフトは最低時間を満たす(self):
        shop = build()
        res = ShiftSolver(shop, time_limit_sec=30).solve()

        assert res.feasible
        v = verify(shop, res.schedule)
        assert not [x for x in v.violations if x.kind == "under_min_hours"]

    def test_最低時間は緩める候補に出る(self):
        """人を増やさないと物理的に届かないこともある。
        必ず守る側に置いたうえで、外す判断は店長に残す。"""
        shop = build()
        solver = ShiftSolver(shop, time_limit_sec=5)
        keys = {r.key.split(":")[0] for r in solver.relaxables}

        assert "minhours" in keys

    def test_法令は緩める候補に出ない(self):
        """最低時間を候補に足したせいで、外してはいけないものまで
        候補に入っていないこと。"""
        shop = build()
        solver = ShiftSolver(shop, time_limit_sec=5)
        labels = " ".join(r.label for r in solver.relaxables)

        for word in ("連勤", "連続勤務", "勤務間隔", "インターバル", "休憩"):
            assert word not in labels
