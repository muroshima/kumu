#!/usr/bin/env python3
"""希望を読んでシフトを組むところまでを通す。

    uv run python scripts/build_shift.py              # 普通の週
    uv run python scripts/build_shift.py --impossible # 人が足りない週
    uv run python scripts/build_shift.py --no-llm     # 希望欄の読み取りを飛ばす

流れ:

    希望欄（自由文）を読む ─ モデル
        ↓ 候補
    指示文が混ざっていないか、日付を特定できたか ─ コード
        ↓ 確認が要るものは店長へ、残りだけ制約になる
    解く ─ ソルバー
        ↓
    解けた   → 通らなかった希望に理由を付ける（もう一度解いて確かめる）
    解けない → 同時に成り立たない条件と、どれを緩めれば解けるかを出す
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kumu.dummy import build  # noqa: E402
from kumu.explain import (  # noqa: E402
    Explainer,
    group_conflicts,
    render_wish_explanation,
    suggest_relaxations,
)
from kumu.llm import LLM, Budget  # noqa: E402
from kumu.model import SLOT_BY_KEY, SLOTS  # noqa: E402
from kumu.report import build_report, save  # noqa: E402
from kumu.solver import ShiftSolver  # noqa: E402
from kumu.translate import Translator, apply_proposals  # noqa: E402

WEEKDAY = ("月", "火", "水", "木", "金", "土", "日")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--impossible", action="store_true", help="人が足りない週で試す")
    parser.add_argument("--no-llm", action="store_true", help="希望欄の読み取りを飛ばす")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--cheap", default="orcarouter/zenken-cheap")
    parser.add_argument("--time-limit", type=float, default=20.0)
    parser.add_argument("--json", default="", help="結果をこのパスに保存する（画面が読む）")
    args = parser.parse_args()

    shop = build(days=args.days, impossible_week=args.impossible)
    print(f"{shop.name}  {shop.start:%Y-%m-%d} から {shop.days} 日")
    print(f"スタッフ {len(shop.staff)}人 / 埋めるコマ {sum(d.total for d in shop.demands)}")
    print()

    # ---------------------------------------------------------- 希望欄を読む
    pending = []
    if not args.no_llm:
        # 「この日は通院があります」の「この日」は、その希望が付いている日を指す。
        # 本文だけでは決まらないので、欄の日付も一緒に渡す
        notes = [(r.staff_id, r.note, r.day) for r in shop.requests if r.note.strip()]
        # 同じ人・同じ文面・同じ日なら結果も同じなので使い回す
        seen: dict[tuple[str, str, object], object] = {}
        llm = LLM(
            budget=Budget(max_calls=120, max_tokens=400_000),
            cache_dir=ROOT / ".cache" / "llm",
        )
        translator = Translator(llm, shop, model=args.cheap)
        proposals = []
        for staff_id, note, day in notes:
            key = (staff_id, note, day)
            if key not in seen:
                seen[key] = translator.translate(staff_id, note, about=day)
            proposals.append(seen[key])

        added, loads, pending = apply_proposals(shop, proposals)
        shop.requests.extend(added)
        shop.load_preferences.extend(loads)

        print("希望欄の読み取り")
        print(f"  読んだ文章        {len(proposals)} 件（呼び出し {llm.ledger.calls} 回）")
        print(f"  制約にした        {len(added)} 件")
        if loads:
            lighter = [lo for lo in loads if lo.level == "lighter"]
            more = [lo for lo in loads if lo.level == "more"]
            parts = []
            if lighter:
                parts.append(f"控えめに {len(lighter)}人")
            if more:
                parts.append(f"もっと入りたい {len(more)}人")
            print(f"  負荷の希望        {' / '.join(parts)}")
        print(f"  店長の確認に回した {len(pending)} 件")
        attacked = [p for p in pending if p.injections]
        if attacked:
            print(f"    うち指示文が混ざっていたもの {len(attacked)} 件:")
            for p in attacked:
                kinds = "・".join(sorted({i["kind"] for i in p.injections}))
                print(f"      {p.staff_name}さん — {kinds}")
                print(f"        「{p.source_note.splitlines()[-1][:52]}」")
        print()

    # ---------------------------------------------------------- 解く
    result = ShiftSolver(shop, time_limit_sec=args.time_limit).solve()

    if not result.feasible:
        print("=" * 68)
        print("このままでは組めません。")
        print("=" * 68)
        print()
        print("同時には成り立たない条件:")
        for line in group_conflicts(result.conflicts):
            print(f"  - {line}")
        print()
        print("どれか1つを動かせば組めます:")
        for s in suggest_relaxations(result.conflicts, shop):
            print(f"  - {s}")
        print()
        print(f"（{result.status} / {result.wall_time_sec}秒）")
        if args.json:
            save(build_report(shop, result, pending=pending), ROOT / args.json)
            print(f"結果を {args.json} に保存しました")
        return 2

    schedule = result.schedule
    assert schedule is not None

    # ---------------------------------------------------------- 出す
    print("=" * 68)
    print(f"組めました（{result.status} / {result.wall_time_sec}秒）")
    print("=" * 68)
    for day in shop.dates:
        print(f"\n{day:%m/%d}（{WEEKDAY[day.weekday()]}）")
        for slot in SLOTS:
            names = [
                f"{shop.staff_by_id(a.staff_id).name}[{a.role.value}]"
                for a in schedule.for_day(day)
                if a.slot_key == slot.key
            ]
            need = shop.demand(day, slot.key)
            mark = f"（必要{need.total}）" if need else ""
            print(f"  {slot.label} {mark:8} {' '.join(names) if names else '—'}")

    print()
    print("-" * 68)
    print(f"人件費 {schedule.labor_cost:,}円 / 割り当て {len(schedule.assignments)} コマ")
    print(f"通らなかった希望 {len(schedule.unmet_wishes)} 件")
    if pending:
        print(f"店長の確認待ち {len(pending)} 件")

    # ---------------------------------------------------------- なぜ通らなかったか
    if schedule.unmet_wishes:
        print()
        print("=" * 68)
        print("通らなかった希望（解き直して確かめた結果）")
        print("=" * 68)
        explainer = Explainer(shop, schedule, time_limit_sec=8.0)
        for req in schedule.unmet_wishes[:3]:
            print()
            print(render_wish_explanation(explainer.why_not(req), shop))
    if args.json:
        print()
        print("画面用の結果を作っています（希望1件ずつ解き直すので少し待ちます）")

        def progress(i: int, total: int) -> None:
            print(f"  {i}/{total}", end="\r", flush=True)

        report = build_report(shop, result, pending=pending, on_progress=progress)
        save(report, ROOT / args.json)
        print(f"\n結果を {args.json} に保存しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
