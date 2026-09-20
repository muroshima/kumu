"""出来上がったシフトが、守るべきことを本当に守っているかを数える。

ソルバーが返した解は定義上すべて満たしているので、本来この検査は要らない。
要るのは**別のやり方で作ったシフトと比べるとき**で、そこで初めて
「守れているかどうか」が比較できる量になる。

言語モデルに作らせたシフトは、見た目は表になっているが中身は守られていない。
それを主観ではなく件数で示すためのモジュール。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .model import SLOT_BY_KEY, SLOTS, Role, Schedule, Shop, Wish


@dataclass
class Violation:
    kind: str
    detail: str
    staff_id: str = ""
    day: date | None = None


@dataclass
class VerifyResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for v in self.violations:
            out[v.kind] += 1
        return dict(out)

    def summary(self) -> str:
        if self.ok:
            return "違反なし"
        parts = [f"{k} {n}件" for k, n in sorted(self.by_kind().items(), key=lambda kv: -kv[1])]
        return f"違反 {len(self.violations)}件（{' / '.join(parts)}）"


LABEL = {
    "under_staffed": "必要人数を割っている",
    "impossible_day": "入れないと出した日に入っている",
    "two_slots": "同じ日に2コマ入っている",
    "over_weekly_hours": "週の上限時間を超えている",
    "under_min_hours": "契約の最低時間に届いていない",
    "too_many_days": "連勤の上限を超えている",
    "short_rest": "勤務間インターバルが足りない",
    "cannot_do_role": "できない持ち場に入っている",
    "no_veteran": "経験者がいないコマがある",
    "unknown_staff": "存在しない人が割り当てられている",
    "unknown_slot": "存在しないコマが使われている",
}


def verify(shop: Shop, schedule: Schedule) -> VerifyResult:
    """守るべきことを1つずつ数える。"""
    res = VerifyResult()
    rules = shop.rules
    staff_ids = {s.id for s in shop.staff}
    slot_keys = {s.key for s in SLOTS}
    valid_days = set(shop.dates)

    # --- そもそも成り立っているか
    for a in schedule.assignments:
        if a.staff_id not in staff_ids:
            res.violations.append(
                Violation("unknown_staff", f"{a.staff_id} は在籍していない", a.staff_id, a.day)
            )
            continue
        if a.slot_key not in slot_keys or a.day not in valid_days:
            res.violations.append(
                Violation("unknown_slot", f"{a.day} {a.slot_key} は対象外", a.staff_id, a.day)
            )
            continue
        if not shop.staff_by_id(a.staff_id).can(a.role):
            res.violations.append(
                Violation(
                    "cannot_do_role",
                    f"{shop.staff_by_id(a.staff_id).name}さんは{a.role.value}に入れない",
                    a.staff_id,
                    a.day,
                )
            )

    valid = [
        a
        for a in schedule.assignments
        if a.staff_id in staff_ids and a.slot_key in slot_keys and a.day in valid_days
    ]

    # --- 必要人数
    for demand in shop.demands:
        for role, need in demand.required.items():
            got = sum(
                1
                for a in valid
                if a.day == demand.day and a.slot_key == demand.slot_key and a.role == role
            )
            if got < need:
                res.violations.append(
                    Violation(
                        "under_staffed",
                        f"{demand.day:%m/%d}（{SLOT_BY_KEY[demand.slot_key].label}）の"
                        f"{role.value}が{got}人（{need}人必要）",
                        day=demand.day,
                    )
                )

    # --- 本人が不可と出した日
    for req in shop.requests:
        if req.wish is not Wish.IMPOSSIBLE:
            continue
        if any(
            a.staff_id == req.staff_id and a.day == req.day and a.slot_key == req.slot_key
            for a in valid
        ):
            res.violations.append(
                Violation(
                    "impossible_day",
                    f"{shop.staff_by_id(req.staff_id).name}さんが入れないと出した "
                    f"{req.day:%m/%d}（{SLOT_BY_KEY[req.slot_key].label}）に入っている",
                    req.staff_id,
                    req.day,
                )
            )

    # --- 1日のコマ数 / 週の時間 / 連勤 / インターバル
    by_staff: dict[str, list] = defaultdict(list)
    for a in valid:
        by_staff[a.staff_id].append(a)

    for staff in shop.staff:
        mine = sorted(by_staff.get(staff.id, []), key=lambda a: (a.day, a.slot_key))

        per_day: dict[date, list] = defaultdict(list)
        for a in mine:
            per_day[a.day].append(a)
        for day, items in per_day.items():
            if len(items) > rules.max_slots_per_day:
                res.violations.append(
                    Violation(
                        "two_slots",
                        f"{staff.name}さんが {day:%m/%d} に{len(items)}コマ入っている",
                        staff.id,
                        day,
                    )
                )

        hours = sum(SLOT_BY_KEY[a.slot_key].hours for a in mine)
        if hours > staff.max_hours_per_week:
            res.violations.append(
                Violation(
                    "over_weekly_hours",
                    f"{staff.name}さんが {hours}時間（契約は{staff.max_hours_per_week}時間まで）",
                    staff.id,
                )
            )
        # 上限だけ見て下限を見ないと、必要人数ぶんだけ埋めて
        # 「制約を満たしている」ことになってしまう。働く側にとっては
        # 約束した時間がもらえないという、上限超過と同じくらい重い問題
        if staff.min_hours_per_week and hours < staff.min_hours_per_week:
            res.violations.append(
                Violation(
                    "under_min_hours",
                    f"{staff.name}さんが {hours}時間"
                    f"（契約は最低{staff.min_hours_per_week}時間）",
                    staff.id,
                )
            )

        limit = min(rules.max_days_in_a_row, staff.max_days_in_a_row)
        worked = sorted(per_day.keys())
        run = longest = 0
        prev = None
        for d in worked:
            run = run + 1 if prev and (d - prev).days == 1 else 1
            longest = max(longest, run)
            prev = d
        if longest > limit:
            res.violations.append(
                Violation(
                    "too_many_days",
                    f"{staff.name}さんが {longest}日連続（上限{limit}日）",
                    staff.id,
                )
            )

        # 1日1コマを前提に日付をキーにすると、同じ日に2コマ入っている
        # 不正な出力で片方が上書きされ、翌日とのインターバル違反を見落とす。
        # 比較の数字が過少になるので、その日の全コマを持つ
        slots_of: dict[date, list[str]] = defaultdict(list)
        for a in mine:
            slots_of[a.day].append(a.slot_key)

        for d in worked:
            nxt = d.fromordinal(d.toordinal() + 1)
            if nxt not in slots_of:
                continue
            for end_key in slots_of[d]:
                for start_key in slots_of[nxt]:
                    a_end = SLOT_BY_KEY[end_key].end_hour
                    b_start = SLOT_BY_KEY[start_key].start_hour
                    rest = (24 - a_end) + b_start
                    if rest < rules.min_rest_hours:
                        res.violations.append(
                            Violation(
                                "short_rest",
                                f"{staff.name}さんの {d:%m/%d}（"
                                f"{SLOT_BY_KEY[end_key].label}）→ {nxt:%m/%d}（"
                                f"{SLOT_BY_KEY[start_key].label}）の間隔が{rest}時間"
                                f"（{rules.min_rest_hours}時間必要）",
                                staff.id,
                                d,
                            )
                        )

    # --- 経験者
    if rules.veteran_required_per_slot:
        veterans = {s.id for s in shop.staff if s.is_veteran}
        for demand in shop.demands:
            if demand.total <= 0:
                continue
            on = [
                a
                for a in valid
                if a.day == demand.day and a.slot_key == demand.slot_key and a.staff_id in veterans
            ]
            if not on:
                res.violations.append(
                    Violation(
                        "no_veteran",
                        f"{demand.day:%m/%d}（{SLOT_BY_KEY[demand.slot_key].label}）に経験者がいない",
                        day=demand.day,
                    )
                )

    return res


def wish_stats(shop: Shop, schedule: Schedule) -> dict[str, int]:
    """希望をどれだけ通せたか。制約ではないので違反には数えないが、
    必要人数だけ埋めたシフトと、希望まで見たシフトの差はここに出る。"""
    assigned = {(a.staff_id, a.day, a.slot_key) for a in schedule.assignments}
    want = [r for r in shop.requests if r.wish is Wish.WANT]
    avoid = [r for r in shop.requests if r.wish is Wish.AVOID]
    return {
        "want_total": len(want),
        "want_met": sum(
            1 for r in want if (r.staff_id, r.day, r.slot_key) in assigned
        ),
        "avoid_total": len(avoid),
        "avoid_violated": sum(
            1 for r in avoid if (r.staff_id, r.day, r.slot_key) in assigned
        ),
    }


def unmet_wants(shop: Shop, schedule: Schedule) -> int:
    """通らなかった「入りたい」の件数。"""
    assigned = {(a.staff_id, a.day, a.slot_key) for a in schedule.assignments}
    return sum(
        1
        for r in shop.requests
        if r.wish is Wish.WANT and (r.staff_id, r.day, r.slot_key) not in assigned
    )


def labor_cost(shop: Shop, schedule: Schedule) -> int:
    total = 0
    for a in schedule.assignments:
        try:
            total += shop.staff_by_id(a.staff_id).hourly_wage * SLOT_BY_KEY[a.slot_key].hours
        except KeyError:
            continue
    return total
