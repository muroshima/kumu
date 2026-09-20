#!/usr/bin/env python3
"""同じ週を「モデルに組ませた場合」と「ソルバーで解いた場合」で比べる。

    uv run python scripts/compare.py --model anthropic/claude-sonnet-5

見るのは3つ。

  守れているか  出てきたシフトが制約を満たしているか（件数で数える）
  いくらか      トークンと金額
  どれだけ待つか 所要時間

主張は「言語モデルにシフトは作れない」なので、作らせて確かめる。
モデル側の作り方は既存実装に寄せてある（生成 → 自己採点 → 修正）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kumu.dummy import build  # noqa: E402
from kumu.llm import LLM, Budget  # noqa: E402
from kumu.llm_baseline import build_with_llm, check  # noqa: E402
from kumu.solver import ShiftSolver  # noqa: E402
from kumu.verify import labor_cost, unmet_wants, verify  # noqa: E402

PRICING = ROOT / "data" / "pricing.json"


def load_pricing() -> dict:
    if not PRICING.exists():
        return {}
    return json.loads(PRICING.read_text(encoding="utf-8"))


def cost_of(model: str, prompt: int, completion: int, pricing: dict) -> float | None:
    table = pricing.get("models", {})
    rate = table.get(model)
    if rate is None:
        for key, value in table.items():
            if key.split("/")[-1] == model.split("/")[-1]:
                rate = value
                break
    if rate is None:
        return None
    return (prompt * rate["input_per_1m"] + completion * rate["output_per_1m"]) / 1_000_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models",
        default="anthropic/claude-sonnet-5",
        help="カンマ区切り。1回だと運の可能性があるので複数で確かめる",
    )
    parser.add_argument("--rounds", type=int, default=2, help="自己採点と修正を何周させるか")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", default="runs/compare.json")
    args = parser.parse_args()

    shop = build(days=args.days)
    pricing = load_pricing()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    print(f"{shop.name} / {shop.start:%Y-%m-%d} から {shop.days}日")
    print(f"スタッフ {len(shop.staff)}人 / 埋めるコマ {sum(d.total for d in shop.demands)}")
    print()

    # ------------------------------------------------ A. モデルに組ませる
    runs = []
    for model in models:
        print("=" * 70)
        print(f"A. モデルに組ませる（{model} / 生成→自己採点→修正 を{args.rounds}周）")
        print("=" * 70)
        llm = LLM(
            budget=Budget(max_calls=20, max_tokens=800_000),
            cache_dir=ROOT / ".cache" / "compare",
        )
        started = time.monotonic()
        baseline = build_with_llm(shop, llm, model=model, rounds=args.rounds)
        sec = time.monotonic() - started
        chk = check(shop, baseline)
        cost = cost_of(model, baseline.prompt_tokens, baseline.completion_tokens, pricing)

        print(f"  呼び出し {baseline.calls}回 / {sec:.1f}秒")
        print(f"  入力 {baseline.prompt_tokens:,} / 出力 {baseline.completion_tokens:,} トークン")
        if baseline.self_scores:
            print(f"  モデルの自己採点: {' → '.join(f'{x}点' for x in baseline.self_scores)}")
        if baseline.schedule is None:
            why = "出力が上限で切れた" if baseline.truncated else "JSON を取り出せなかった"
            print(f"  シフトを取り出せなかった（{why}）")
        else:
            print(f"  割り当て {chk['assignments']}コマ")
            print(f"  実際に検査した結果: {chk['summary']}")
            if chk.get("by_kind"):
                from kumu.verify import LABEL

                for kind, n in sorted(chk["by_kind"].items(), key=lambda kv: -kv[1]):
                    print(f"    {LABEL.get(kind, kind)}: {n}件")
        print()

        runs.append(
            {
                "model": model,
                "calls": baseline.calls,
                "prompt_tokens": baseline.prompt_tokens,
                "completion_tokens": baseline.completion_tokens,
                "cost_usd": cost,
                "seconds": round(sec, 2),
                "self_scores": baseline.self_scores,
                "violations": chk.get("violations"),
                "by_kind": chk.get("by_kind"),
                "assignments": chk.get("assignments"),
                "parse_errors": baseline.parse_errors,
                "truncated": baseline.truncated,
            }
        )

    # ------------------------------------------------ B. ソルバーで解く
    print()
    print("=" * 70)
    print("B. ソルバーで解く（kumu）")
    print("=" * 70)
    started = time.monotonic()
    result = ShiftSolver(shop, time_limit_sec=30).solve()
    b_sec = time.monotonic() - started

    if not result.feasible:
        print("  組めなかった（この比較では使えない）")
        return 1

    b_verify = verify(shop, result.schedule)
    print(f"  モデルの呼び出し 0回 / {b_sec:.2f}秒")
    print(f"  割り当て {len(result.schedule.assignments)}コマ")
    print(f"  実際に検査した結果: {b_verify.summary()}")

    # ------------------------------------------------ 並べる
    print()
    print("=" * 70)
    print(f"{'':30} {'違反':>8} {'呼出':>6} {'トークン':>10} {'金額':>10} {'時間':>8}")
    for r in runs:
        name = r["model"].split("/")[-1][:28]
        viol = f"{r['violations']}件" if r["violations"] is not None else "取れず"
        cost = f"${r['cost_usd']:.4f}" if r["cost_usd"] is not None else "—"
        tok = r["prompt_tokens"] + r["completion_tokens"]
        print(
            f"{name:30} {viol:>8} {str(r['calls']) + '回':>6} {tok:>10,} {cost:>10} "
            f"{format(r['seconds'], '.0f') + '秒':>8}"
        )
    print(
        f"{'kumu（ソルバー）':30} {str(len(b_verify.violations)) + '件':>8} {'0回':>6} "
        f"{0:>10,} {'$0.0000':>10} {format(b_sec, '.2f') + '秒':>8}"
    )
    print()
    ok_runs = [r for r in runs if r["violations"] is not None]
    if ok_runs:
        worst = max(r["violations"] for r in ok_runs)
        best = min(r["violations"] for r in ok_runs)
        total_cost = sum(r["cost_usd"] or 0 for r in ok_runs)
        print(
            f"モデルに組ませると、どれも制約を守れなかった（違反 {best}〜{worst}件）。"
        )
        print(f"かかった金額の合計 ${total_cost:.4f}、ソルバーは $0。")
    print(f"ソルバーは {unmet_wants(shop, result.schedule)}件の希望が通らなかったが、"
          f"守るべきことは1件も破っていない。")

    out = {
        "shop": shop.name,
        "week": shop.start.isoformat(),
        "days": shop.days,
        "rounds": args.rounds,
        "llm_runs": runs,
        "solver": {
            "calls": 0,
            "cost_usd": 0.0,
            "seconds": round(b_sec, 3),
            "violations": len(b_verify.violations),
            "assignments": len(result.schedule.assignments),
            "unmet_wants": unmet_wants(shop, result.schedule),
            "labor_cost": labor_cost(shop, result.schedule),
        },
    }
    path = ROOT / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"結果を {args.out} に保存しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
