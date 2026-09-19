"""画面から出されたものが、組み立てに届いているかのテスト。

画面で希望を出せても、組むときに読まなければ何も起きない。
「動いて見えるが繋がっていない」を防ぐための検証。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kumu.dummy import build  # noqa: E402
from kumu.inbox import apply_submissions, apply_trust  # noqa: E402
from kumu.model import Role, Wish  # noqa: E402
from kumu.solver import ShiftSolver  # noqa: E402
from kumu.trust import append_event  # noqa: E402

WEEK = date(2026, 10, 5)


def write(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    return path


def submission(**over) -> dict:
    base = {
        "week": WEEK.isoformat(),
        "staff_id": "S01",
        "staff_name": "田中 陽介",
        "picks": [],
        "note": "",
        "kind": "unclear",
        "days": [],
        "slots": [],
        "load": "normal",
        "reason": "",
        "confidence": 0.0,
        "injections": [],
        "needs_human": False,
        "ack_key": "k1",
        "at": "2026-09-20T03:00:00",
    }
    return {**base, **over}


class Test画面で選んだ時間帯:
    def test_そのまま希望になる(self, tmp_path):
        shop = build(start=WEEK)
        before = len(shop.requests)
        path = write(
            tmp_path / "s.jsonl",
            [
                submission(
                    picks=[
                        {"day": "2026-10-06", "slot": "early", "state": "impossible", "role": ""}
                    ]
                )
            ],
        )

        added, _loads = apply_submissions(shop, path)

        assert added == 1
        assert len(shop.requests) == before + 1
        got = shop.requests[-1]
        assert got.staff_id == "S01"
        assert got.day == date(2026, 10, 6)
        assert got.wish is Wish.IMPOSSIBLE

    def test_持ち場の指定も届く(self, tmp_path):
        shop = build(start=WEEK)
        path = write(
            tmp_path / "s.jsonl",
            [
                submission(
                    picks=[
                        {"day": "2026-10-06", "slot": "mid", "state": "want", "role": "ホール"}
                    ]
                )
            ],
        )

        apply_submissions(shop, path)

        assert shop.requests[-1].role is Role.HALL

    def test_選んだ日に割り当てられない(self, tmp_path):
        """取り込んだ希望が、解いた結果に効いているか。"""
        day = date(2026, 10, 6)
        shop = build(start=WEEK)
        path = write(
            tmp_path / "s.jsonl",
            [
                submission(
                    picks=[
                        {"day": day.isoformat(), "slot": s, "state": "impossible", "role": ""}
                        for s in ("early", "mid", "late")
                    ]
                )
            ],
        )
        apply_submissions(shop, path)

        res = ShiftSolver(shop, time_limit_sec=25).solve()

        assert res.feasible
        assert not [
            a for a in res.schedule.assignments if a.staff_id == "S01" and a.day == day
        ]

    def test_別の週に出された希望は混ざらない(self, tmp_path):
        shop = build(start=WEEK)
        path = write(
            tmp_path / "s.jsonl",
            [
                submission(
                    week=(WEEK + timedelta(days=7)).isoformat(),
                    picks=[
                        {"day": "2026-10-13", "slot": "early", "state": "impossible", "role": ""}
                    ],
                )
            ],
        )

        added, _loads = apply_submissions(shop, path)

        assert added == 0

    def test_期間外の日付は捨てる(self, tmp_path):
        shop = build(start=WEEK)
        path = write(
            tmp_path / "s.jsonl",
            [
                submission(
                    picks=[
                        {"day": "2026-12-25", "slot": "early", "state": "impossible", "role": ""}
                    ]
                )
            ],
        )

        added, _loads = apply_submissions(shop, path)

        assert added == 0


class Test自由文から読み取ったぶん:
    def test_確認が済んでいなければ取り込まない(self, tmp_path):
        shop = build(start=WEEK)
        path = write(
            tmp_path / "s.jsonl",
            [
                submission(
                    kind="impossible",
                    days=["2026-10-06"],
                    needs_human=True,
                    injections=[{"kind": "優先扱いの要求", "matched": "最優先"}],
                )
            ],
        )

        added, _loads = apply_submissions(shop, path)

        assert added == 0, "確認が済んでいないものが制約になっている"

    def test_店長が承認したものは取り込む(self, tmp_path):
        shop = build(start=WEEK)
        path = write(
            tmp_path / "s.jsonl",
            [
                submission(
                    kind="impossible", days=["2026-10-06"], slots=["late"], needs_human=True
                )
            ],
        )

        added, _loads = apply_submissions(
            shop, path, decisions={"k1": {"action": "accept"}}
        )

        assert added == 1

    def test_店長が却下したものは取り込まない(self, tmp_path):
        shop = build(start=WEEK)
        path = write(
            tmp_path / "s.jsonl",
            [submission(kind="impossible", days=["2026-10-06"], needs_human=False)],
        )

        added, _loads = apply_submissions(
            shop, path, decisions={"k1": {"action": "reject"}}
        )

        assert added == 0

    def test_負荷の希望も届く(self, tmp_path):
        shop = build(start=WEEK)
        path = write(tmp_path / "s.jsonl", [submission(load="lighter", reason="掛け持ち")])

        _added, loads = apply_submissions(shop, path)

        assert loads == 1
        assert shop.load_level("S01") == "lighter"


class Test信頼ポイント:
    def test_記録が重みに反映される(self, tmp_path):
        shop = build(start=WEEK)
        path = tmp_path / "t.jsonl"
        before = shop.staff_by_id("S01").trust
        append_event(path, "S01", "no_show")

        applied = apply_trust(shop, path)

        assert "S01" in applied
        assert shop.staff_by_id("S01").trust < before

    def test_記録が無い人は変わらない(self, tmp_path):
        shop = build(start=WEEK)
        path = tmp_path / "t.jsonl"
        append_event(path, "S01", "late")
        before = shop.staff_by_id("S02").trust

        apply_trust(shop, path)

        assert shop.staff_by_id("S02").trust == before

    def test_信頼が低くても契約の最低時間は割り当てる(self, tmp_path):
        """点数を下げることと、働けなくすることは別。"""
        shop = build(start=WEEK)
        path = tmp_path / "t.jsonl"
        for _ in range(6):
            append_event(path, "S01", "no_show")
        apply_trust(shop, path)
        target = shop.staff_by_id("S01")
        assert target.trust <= 60, "前提の信頼度が下がっていない"

        res = ShiftSolver(shop, time_limit_sec=25).solve()

        assert res.feasible
        hours = sum(
            {"early": 6, "mid": 6, "late": 6}[a.slot_key]
            for a in res.schedule.assignments
            if a.staff_id == "S01"
        )
        assert hours >= target.min_hours_per_week, (
            f"信頼が低いというだけで契約の下限({target.min_hours_per_week}h)を割った: {hours}h"
        )

    def test_点数には下限がある(self, tmp_path):
        """際限なく下げると、実質的に解雇と同じになる。"""
        from kumu.trust import MIN_TRUST, load_events, scores

        path = tmp_path / "t.jsonl"
        for _ in range(50):
            append_event(path, "S01", "no_show")

        assert scores(load_events(path))["S01"] == MIN_TRUST
