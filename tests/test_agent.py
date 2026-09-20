"""組めなかったときに自分で手を打つ部分のテスト。

ここで確かめたいのは、打つ手が**正しい範囲に収まっているか**。
組めさえすればいいなら、法令を外せばたいてい組める。それをやらないことが
この機能の条件になる。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kumu.agent import PRIORITY, run  # noqa: E402
from kumu.dummy import build  # noqa: E402
from kumu.model import SLOTS, Demand, Request, Role, Rules, Shop, Staff, Wish  # noqa: E402
from kumu.verify import verify  # noqa: E402

START = date(2026, 10, 5)


def tight_shop() -> Shop:
    """キッチンに入れるのが1人しかいない、詰みかけの店。"""
    staff = [
        Staff("A", "あさひ", [Role.HALL], 1200, True, 40, 0),
        Staff("B", "ばん", [Role.HALL], 1100, True, 40, 0),
        Staff("C", "ちひろ", [Role.HALL], 1000, False, 40, 0),
        Staff("K", "きっちん", [Role.KITCHEN], 1000, True, 40, 0),
    ]
    demands = []
    for i in range(2):
        for slot in SLOTS:
            need = {Role.HALL: 1}
            if slot.key == "mid":
                need[Role.KITCHEN] = 1
            demands.append(Demand(START + timedelta(days=i), slot.key, need))
    return Shop(
        name="テスト店",
        start=START,
        days=2,
        staff=staff,
        demands=demands,
        requests=[],
        rules=Rules(veteran_required_per_slot=False),
    )


class Test組めるときは何もしない:
    def test_1手で終わる(self):
        shop = build()
        res = run(shop, time_limit_sec=25)

        assert res.feasible
        assert len(res.steps) == 1, "組めているのに緩和を試している"
        assert res.proposal is False
        assert res.applied == []


class Test組めないときに手を打つ:
    def test_緩和して組める形を見つける(self):
        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8)

        assert res.feasible, "打てる手があるのに組めないままになっている"
        assert len(res.steps) > 1
        assert res.applied, "何を外したかを記録していない"

    def test_確定ではなく提案として返す(self):
        """ルールを外して組んだシフトを、勝手に確定させない。"""
        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8)

        assert res.feasible
        assert res.proposal is True, "外したのに提案として印を付けていない"

    def test_打った手が全部記録に残る(self):
        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8)

        # 最初の1手（そのまま解く）を除いた回数と、外した件数が一致する
        assert len(res.steps) - 1 == len(res.applied)
        for step in res.steps:
            assert step.action, "何をしたかが空になっている"

    def test_緩和して組めた解も制約を満たす(self):
        """緩めたぶん以外は守られていること。"""
        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8)
        assert res.feasible

        v = verify(shop, res.result.schedule)
        # 緩めた条件（必要人数と経験者）以外の違反が出ていないこと
        allowed = {"under_staffed", "no_veteran"}
        unexpected = [x for x in v.violations if x.kind not in allowed]
        assert not unexpected, f"緩めていない条件を破っている: {[x.detail for x in unexpected]}"

    def test_試せる回数を超えたら諦めて人に返す(self):
        """必要人数を下げ続ければいつかは組めてしまう。

        延々と緩め続けて「組めました」と言う方が有害なので、回数で止める。
        止まったときは、組めなかったことと試した記録を返す。
        """
        shop = build(impossible_week=True)

        res = run(shop, time_limit_sec=10, max_attempts=1)

        assert not res.feasible, "1手で組めるはずのない状況で組めたことになっている"
        assert len(res.steps) == 2, "手を打っていない、または止まっていない"
        assert res.result.conflicts, "組めない理由を返していない"


class Test打ってよい手の範囲:
    def test_法令は緩和候補に出てこない(self):
        """連勤や勤務間隔を外して組めても、それは組めたことにしない。"""
        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8)

        for step in res.steps[1:]:
            for word in ("連勤", "連続勤務", "勤務間隔", "インターバル", "休憩"):
                assert word not in step.action, f"法令を緩めようとしている: {step.action}"

    def test_店が決められることから先に試す(self):
        """人に頼み直すより、店の基準を下げる方が先。"""
        assert PRIORITY["veteran"] < PRIORITY["demand"] < PRIORITY["ng"]

    def test_本人への確認が要ることは印が付く(self):
        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8)

        for step in res.steps:
            if "不可を守る" in step.action:
                assert "確認" in step.action, "本人に聞く必要があることを伝えていない"


class Test時間切れと組めないは別:
    """時間切れは「組めない」ではない。まだ分かっていないだけ。

    現場では打つ手がまったく違う。組めない週は条件を見直す話で、
    分からない週は待つか範囲を狭める話になる。

    実際に時間切れを起こさせるテストは、前処理だけで矛盾が見つかる週だと
    一瞬で INFEASIBLE が返り、実行するたびに結果が変わってしまう。
    そこで、時間切れの状態を作って渡し、そこからの振る舞いを見る。
    """

    def test_矛盾を挙げるのは組めないと分かったときだけ(self):
        from kumu.solver import ShiftSolver

        for limit in (0.001, 15.0):
            res = ShiftSolver(build(impossible_week=True), time_limit_sec=limit).solve()
            if res.feasible:
                continue
            if res.timed_out:
                assert res.conflicts == [], "判断できていないのに矛盾を挙げている"
                assert res.undecided
            else:
                assert res.status == "INFEASIBLE"
                assert res.conflicts, "組めないと分かったのに理由を返していない"

    def test_時間切れのときは制約を緩めない(self, monkeypatch):
        """何が悪いか分かっていないのに条件を外すのが、一番やってはいけないこと。"""
        from kumu import agent as agent_mod
        from kumu.solver import SolveResult

        def timed_out(self):
            return SolveResult(
                schedule=None, feasible=False, status="UNKNOWN", timed_out=True
            )

        monkeypatch.setattr(agent_mod.ShiftSolver, "solve", timed_out)
        res = run(build(impossible_week=True), time_limit_sec=1, max_attempts=6)

        assert not res.feasible
        assert res.applied == [], "判断できていないのに条件を外している"
        assert res.proposal is False
        assert len(res.steps) == 1, "判断できていないのに手を打っている"

    def test_組めない週では従来どおり手を打つ(self):
        """時間切れの扱いを足したせいで、本来の動きが止まっていないこと。"""
        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8)

        assert not res.result.timed_out
        assert res.applied, "組めない週で手を打たなくなっている"


class Test契約の見直しは最後に回す:
    def test_店の基準より後ろに置く(self):
        """店が自分で決められること → 人に相談 → 契約の見直し、の順。"""
        assert PRIORITY["veteran"] < PRIORITY["demand"] < PRIORITY["ng"] < PRIORITY["minhours"]

    def test_外したら実際に条件が変わる(self):
        """「外した」と言いながら何も変わっていない、が起きないこと。"""
        from kumu.agent import _apply
        from kumu.solver import Relaxable, ShiftSolver

        shop = build()
        target = next(s for s in shop.staff if s.min_hours_per_week > 0)
        solver = ShiftSolver(shop, time_limit_sec=5)
        r = next(x for x in solver.relaxables if x.key == f"minhours:{target.id}")

        after = _apply(shop, r)

        assert after.staff_by_id(target.id).min_hours_per_week == 0
        assert shop.staff_by_id(target.id).min_hours_per_week > 0, "元の店を書き換えている"
        assert [s.id for s in after.staff] == [s.id for s in shop.staff]

    def test_本人の合意が要ることが文言に出る(self):
        from kumu.agent import _relax_label
        from kumu.solver import Relaxable, ShiftSolver

        shop = build()
        target = next(s for s in shop.staff if s.min_hours_per_week > 0)
        solver = ShiftSolver(shop, time_limit_sec=5)
        r = next(x for x in solver.relaxables if x.key == f"minhours:{target.id}")

        assert "契約" in _relax_label(r)
