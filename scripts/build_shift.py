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
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kumu.dummy import build  # noqa: E402
from kumu.agent import describe, run as run_agent  # noqa: E402
from kumu.inbox import apply_submissions, apply_trust  # noqa: E402
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

# 希望欄の読み取りに使うモデル。OrcaRouter の Named Router 名でも、
# プロバイダのモデル名でもよい。環境変数で差し替えられる
CHEAP_MODEL = os.environ.get("KUMU_MODEL", "orcarouter/zenken-cheap")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--impossible", action="store_true", help="人が足りない週で試す")
    parser.add_argument("--no-llm", action="store_true", help="希望欄の読み取りを飛ばす")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--start", default="", help="組む週の開始日（YYYY-MM-DD）。月曜に丸められる"
    )
    parser.add_argument(
        "--next", action="store_true",
        help="いま募集している週（runs/result.json の次の週）を組む"
    )
    parser.add_argument("--cheap", default=CHEAP_MODEL)
    parser.add_argument("--time-limit", type=float, default=20.0)
    parser.add_argument("--json", default="", help="結果をこのパスに保存する（画面が読む）")
    parser.add_argument(
        "--no-retry", action="store_true",
        help="組めなかったときに緩和案を試さない（1回解いて終わり）",
    )
    parser.add_argument(
        "--ignore-inbox", action="store_true", help="画面から出された希望と実績を読まない"
    )
    args = parser.parse_args()

    start = None
    if args.next:
        # 画面が「募集中」として希望を集めている週を組む。
        # ここがずれると、出された希望が1件も取り込まれない
        import json as _json

        prev = ROOT / "runs" / "result.json"
        if prev.exists():
            cur = _json.loads(prev.read_text(encoding="utf-8"))
            start = date.fromisoformat(cur["start"]) + timedelta(days=int(cur["days"]))
    elif args.start:
        start = date.fromisoformat(args.start)

    shop = build(days=args.days, start=start, impossible_week=args.impossible)
    print(f"{shop.name}  {shop.start:%Y-%m-%d} から {shop.days} 日")
    print(f"スタッフ {len(shop.staff)}人 / 埋めるコマ {sum(d.total for d in shop.demands)}")
    print()

    # ---------------------------------------------------------- 画面からの入力
    if not args.ignore_inbox:
        decisions = {}
        dec_path = ROOT / "runs" / "decisions.json"
        if dec_path.exists():
            import json as _json

            try:
                decisions = _json.loads(dec_path.read_text(encoding="utf-8"))
            except _json.JSONDecodeError:
                decisions = {}

        added, loads = apply_submissions(
            shop, ROOT / "runs" / "submissions.jsonl", decisions=decisions
        )
        moved = apply_trust(shop, ROOT / "runs" / "trust.jsonl")
        if added or loads or moved:
            print("画面から取り込んだもの")
            if added:
                print(f"  希望            {added} 件")
            if loads:
                print(f"  負荷の希望      {loads} 人")
            if moved:
                names = "・".join(
                    f"{shop.staff_by_id(k).name}({v})" for k, v in list(moved.items())[:5]
                )
                print(f"  信頼ポイント    {len(moved)}人 — {names}")
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
    if args.no_retry:
        result = ShiftSolver(shop, time_limit_sec=args.time_limit).solve()
        agent = None
    else:
        # 組めなかったらそこで止まらず、緩められる条件を順に試す
        def on_step(step) -> None:
            mark = "組めた" if step.feasible else f"組めない（矛盾 {step.conflicts}件）"
            print(f"  {step.action} → {mark}", flush=True)

        print("組んでいます")
        agent = run_agent(
            shop, time_limit_sec=args.time_limit, max_attempts=8, on_step=on_step
        )
        result = agent.result
        print()
        if agent.proposal:
            print("そのままでは組めなかったので、次を外せば組めることを確かめました:")
            for a in agent.applied:
                print(f"  - {a}")
            print("外してよいかは店長が決めてください。これは提案で、確定ではありません。")
            print()

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
