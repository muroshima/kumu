"""組めなかったときに自分で手を打つ部分のテスト。

ここで確かめたいのは、打つ手が**正しい範囲に収まっているか**。
組めさえすればいいなら、法令を外せばたいてい組める。それをやらないことが
この機能の条件になる。
"""

from __future__ import annotations

import pytest
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kumu.agent import PRIORITY, describe, run  # noqa: E402
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


class TestAIに選ばせる:
    """どの条件からゆずるかは AI が決める。ただし決めてよい範囲と、
    決めた結果の扱いは、こちらが押さえておく。"""

    def _advisor(self, reply: str):
        from kumu.advisor import Advisor

        class 決め打ちLLM:
            def complete(self, *a, **kw):
                return {"content": reply, "usage": {}}

        return Advisor(決め打ちLLM())

    def test_AIが選んだ手が使われる(self):
        from kumu.solver import ShiftSolver

        shop = build(impossible_week=True)
        first = ShiftSolver(shop, time_limit_sec=20).solve()
        assert not first.feasible
        # 既定の順では最初に選ばれない候補を、わざと指名させる
        target = sorted(first.conflicts, key=lambda r: r.key)[-1]

        res = run(
            shop,
            time_limit_sec=15,
            max_attempts=6,
            advisor=self._advisor(
                '{"key": "%s", "reason": "この日は代わりが効かないため"}' % target.key
            ),
        )

        assert res.steps[1].chosen_by == "AI"
        assert "代わりが効かない" in res.steps[1].reason

    def test_候補にないものを返してきたら従わない(self):
        """AI が勝手な条件を返しても、そこにない手は打たない。"""
        shop = build(impossible_week=True)
        res = run(
            shop,
            time_limit_sec=15,
            max_attempts=6,
            advisor=self._advisor('{"key": "連勤の上限を外す", "reason": "そのほうが早い"}'),
        )

        assert res.steps[1].chosen_by == "既定の順"
        for step in res.steps[1:]:
            for word in ("連勤", "勤務間隔", "インターバル", "休憩"):
                assert word not in step.action

    def test_AIが落ちても止まらない(self):
        """外の呼び出しが落ちても、決め打ちの順で最後まで進む。"""
        from kumu.advisor import Advisor

        class 落ちるLLM:
            def complete(self, *a, **kw):
                raise ConnectionError("gateway unreachable")

        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8, advisor=Advisor(落ちるLLM()))

        assert res.feasible, "AI が落ちただけで組めなくなっている"
        assert all(s.chosen_by in ("", "既定の順") for s in res.steps)

    def test_壊れた返事でも止まらない(self):
        shop = build(impossible_week=True)
        res = run(
            shop, time_limit_sec=15, max_attempts=8,
            advisor=self._advisor("すみません、よく分かりませんでした"),
        )

        assert res.feasible
        assert res.steps[1].chosen_by == "既定の順"

    def test_AIが選んでも確定はしない(self):
        from kumu.solver import ShiftSolver

        shop = build(impossible_week=True)
        first = ShiftSolver(shop, time_limit_sec=20).solve()
        target = sorted(first.conflicts, key=lambda r: r.key)[0]
        res = run(
            shop, time_limit_sec=15, max_attempts=8,
            advisor=self._advisor('{"key": "%s", "reason": "痛みが小さいため"}' % target.key),
        )

        assert res.proposal is True, "AI の判断がそのまま確定になっている"

    def test_AIが選んでも緩めていない条件は守る(self):
        """AI に選ばせたぶん、守るべきものが崩れていないこと。"""
        from kumu.solver import ShiftSolver
        from kumu.verify import verify

        shop = build(impossible_week=True)
        first = ShiftSolver(shop, time_limit_sec=20).solve()
        target = sorted(first.conflicts, key=lambda r: r.key)[0]
        res = run(
            shop, time_limit_sec=15, max_attempts=8,
            advisor=self._advisor('{"key": "%s", "reason": "x"}' % target.key),
        )
        assert res.feasible

        # ゆずった条件（必要人数・経験者・本人の不可・契約の下限）は破れて当然。
        # 破れてはいけないのは、緩和候補にそもそも出していない法令由来のもの
        v = verify(shop, res.result.schedule)
        never = {"too_many_days", "short_rest", "over_weekly_hours",
                 "two_slots", "cannot_do_role", "unknown_staff", "unknown_slot"}
        broken = [x for x in v.violations if x.kind in never]
        assert not broken, [x.detail for x in broken]

    def test_上限超過は飲み込まない(self):
        from kumu.advisor import Advisor
        from kumu.llm import BudgetExceeded

        class 上限LLM:
            def complete(self, *a, **kw):
                raise BudgetExceeded("上限に達しました")

        shop = build(impossible_week=True)
        with pytest.raises(BudgetExceeded):
            run(shop, time_limit_sec=15, max_attempts=4, advisor=Advisor(上限LLM()))


class TestAIが前の結果を見る:
    """一手ごとに聞き直すだけでは、同じ方向に外し続ける。
    前の手で何が起きたかを見て、次を決められること。"""

    def test_前の結果が次の判断材料として渡る(self):
        from kumu.advisor import Advisor

        seen = []

        class 記録するLLM:
            def complete(self, messages, **kw):
                seen.append(messages[-1]["content"])
                return {"content": '{"key":"STOP","reason":"記録用"}', "usage": {}}

        shop = build(impossible_week=True)
        run(shop, time_limit_sec=15, max_attempts=4, advisor=Advisor(記録するLLM()))

        assert seen, "AI に一度も聞いていない"
        assert "ゆずれる候補" in seen[0]

    def test_増減の向きが文章として渡る(self):
        from kumu.advisor import _history

        text = "\n".join(
            _history(
                [
                    {"action": "A をやめる", "before": 12, "after": 8, "feasible": False},
                    {"action": "B を1人減らす", "before": 8, "after": 58, "feasible": False},
                ]
            )
        )

        assert "減った" in text and "増えた" in text
        assert "この方向は外れ" in text

    def test_AIが止めたら組めたことにしない(self):
        """ゆずり続ければいつかは組める。それを組めたと言わないための判断。"""
        from kumu.advisor import Advisor

        class 止めるLLM:
            def complete(self, *a, **kw):
                return {
                    "content": '{"key":"STOP","reason":"これ以上は店の基準を下げすぎる"}',
                    "usage": {},
                }

        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=8, advisor=Advisor(止めるLLM()))

        assert not res.feasible, "止めたのに組めたことになっている"
        assert res.applied == [], "止めると言いながら条件を外している"
        assert any(s.action == "ここで止める" for s in res.steps)
        assert "基準を下げすぎる" in describe(shop, res)

    def test_STOPは候補になくても受け入れる(self):
        """止めるのは安全側に倒れる判断なので、候補照合の対象にしない。"""
        from kumu.advisor import Advisor
        from kumu.solver import ShiftSolver

        class 止めるLLM:
            def complete(self, *a, **kw):
                return {"content": '{"key":"stop","reason":"小文字でも止める"}', "usage": {}}

        shop = build(impossible_week=True)
        res = run(shop, time_limit_sec=15, max_attempts=6, advisor=Advisor(止めるLLM()))

        assert any(s.action == "ここで止める" for s in res.steps)
        assert res.result.conflicts, "止めたときに理由を返していない"
