"""組んだ結果を、画面から読める形にまとめる。

画面を出すたびに解き直すと待たされるので、ここで一度だけ計算して置いておく。
「なぜ通らなかったか」は希望1件につき解き直すので、まとめて作るのに時間がかかる。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from .explain import Explainer, group_conflicts, suggest_relaxations
from .model import SLOT_BY_KEY, SLOTS, Schedule, Shop
from .solver import SolveResult
from .translate import Proposal

WEEKDAY = ("月", "火", "水", "木", "金", "土", "日")


def _staff(shop: Shop) -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "name": s.name,
            "roles": [r.value for r in s.roles],
            "veteran": s.is_veteran,
            "hourly_wage": s.hourly_wage,
            "trust": s.trust,
            "wish_weight": s.wish_weight,
            "max_hours": s.max_hours_per_week,
            "min_hours": s.min_hours_per_week,
        }
        for s in shop.staff
    ]


def _calendar(shop: Shop, schedule: Schedule) -> list[dict[str, Any]]:
    out = []
    for day in shop.dates:
        slots = []
        for slot in SLOTS:
            demand = shop.demand(day, slot.key)
            assigned = [
                {
                    "staff_id": a.staff_id,
                    "name": shop.staff_by_id(a.staff_id).name,
                    "role": a.role.value,
                    "veteran": shop.staff_by_id(a.staff_id).is_veteran,
                }
                for a in schedule.for_day(day)
                if a.slot_key == slot.key
            ]
            slots.append(
                {
                    "key": slot.key,
                    "label": slot.label,
                    "hours": f"{slot.start_hour}:00-{slot.end_hour}:00",
                    "required": demand.total if demand else 0,
                    "required_detail": (
                        {r.value: n for r, n in demand.required.items() if n} if demand else {}
                    ),
                    "assigned": assigned,
                }
            )
        out.append(
            {
                "date": day.isoformat(),
                "label": f"{day:%m/%d}",
                "weekday": WEEKDAY[day.weekday()],
                "slots": slots,
            }
        )
    return out


def _hours_by_staff(shop: Shop, schedule: Schedule) -> list[dict[str, Any]]:
    out = []
    for s in shop.staff:
        hours = sum(
            SLOT_BY_KEY[a.slot_key].hours for a in schedule.assignments if a.staff_id == s.id
        )
        out.append(
            {
                "name": s.name,
                "hours": hours,
                "min_hours": s.min_hours_per_week,
                "max_hours": s.max_hours_per_week,
                "under_min": hours < s.min_hours_per_week,
            }
        )
    return sorted(out, key=lambda r: -r["hours"])


def build_report(
    shop: Shop,
    result: SolveResult,
    *,
    pending: list[Proposal] | None = None,
    explain_limit: int = 12,
    on_progress=None,
) -> dict[str, Any]:
    """画面が読む JSON を作る。"""
    pending = pending or []
    report: dict[str, Any] = {
        "shop": shop.name,
        "start": shop.start.isoformat(),
        "days": shop.days,
        "feasible": result.feasible,
        # 組めないのか、まだ分かっていないのかは、画面でも書き分ける
        "undecided": result.undecided,
        "solver_status": result.status,
        "solve_sec": result.wall_time_sec,
        "staff": _staff(shop),
        "pending": [
            {
                "staff_name": p.staff_name,
                "note": p.source_note,
                # 画面側が同じ識別子を作れるように、欄の日付も渡す
                "about": p.about.isoformat() if p.about else "",
                "kind": p.kind,
                "days": [d.isoformat() for d in p.days],
                "slots": p.slots,
                "reason": p.reason,
                "confidence": p.confidence,
                "injections": p.injections,
                "error": p.error,
                "why": _pending_reason(p),
            }
            for p in pending
        ],
    }

    if not result.feasible:
        report["conflicts"] = group_conflicts(result.conflicts)
        report["suggestions"] = suggest_relaxations(result.conflicts, shop)
        return report

    schedule = result.schedule
    assert schedule is not None
    report["labor_cost"] = schedule.labor_cost
    report["assignments"] = len(schedule.assignments)
    report["calendar"] = _calendar(shop, schedule)
    report["hours"] = _hours_by_staff(shop, schedule)

    explainer = Explainer(shop, schedule, time_limit_sec=8.0)
    unmet = []
    for i, req in enumerate(schedule.unmet_wishes[:explain_limit], 1):
        if on_progress:
            on_progress(i, min(len(schedule.unmet_wishes), explain_limit))
        exp = explainer.why_not(req)
        entry: dict[str, Any] = {
            "staff_name": shop.staff_by_id(req.staff_id).name,
            "date": req.day.isoformat(),
            "label": f"{req.day:%m/%d}",
            "weekday": WEEKDAY[req.day.weekday()],
            "slot": SLOT_BY_KEY[req.slot_key].label,
            "satisfiable": exp.satisfiable,
        }
        if exp.satisfiable and exp.tradeoff:
            entry["cost_delta"] = exp.tradeoff.cost_delta
            entry["newly_unmet"] = [
                {
                    "name": shop.staff_by_id(o.staff_id).name,
                    "label": f"{o.day:%m/%d}",
                    "slot": SLOT_BY_KEY[o.slot_key].label,
                }
                for o in exp.tradeoff.newly_unmet[:6]
            ]
        else:
            entry["blockers"] = group_conflicts(exp.blockers)
            entry["suggestions"] = suggest_relaxations(exp.blockers, shop)
        unmet.append(entry)

    report["unmet"] = unmet
    report["unmet_total"] = len(schedule.unmet_wishes)
    return report


def _pending_reason(p: Proposal) -> str:
    """なぜ人の確認に回ったかを1文で。"""
    if p.injections:
        kinds = "・".join(sorted({i["kind"] for i in p.injections}))
        return f"希望欄に指示文が混ざっています（{kinds}）"
    if p.error:
        return "読み取りに失敗しました"
    if p.kind == "unclear":
        return "何を希望しているか読み取れませんでした"
    if not p.days:
        return "どの日を指しているか特定できませんでした"
    return f"読み取れましたが確信が持てません（{p.confidence:.1f}）"


def save(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
