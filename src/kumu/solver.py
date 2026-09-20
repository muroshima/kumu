"""シフトを解く。ここに LLM は出てこない。

## なぜソルバーなのか

シフト作成は組合せ最適化で、言語モデルが解ける問題ではない。
出力させれば表は出てくるが、最低人数を割った日ができたり、連勤が伸びたり、
本人が不可と書いた日に入っていたりする。**守れているかどうかは、出してみるまで分からない。**

CP-SAT に解かせると、返ってくる解は制約を必ず満たしている。満たせないなら解を返さない。
「だいたい守れているシフト」が出てこないことが、このやり方の利点になる。

## 解けなかったときに何を言うか

現場で本当に困るのは、どう組んでも人が足りない週だと思う。
言語モデルはそこでも何か出すが、出てきたものは守れない。

ここでは緩められる制約それぞれに仮定リテラルを割り当てておき、解けなかったときに
**どの仮定が同時には成り立たないか**を CP-SAT から受け取る。
「解けません」ではなく「土曜の最低人数を2人から1人にすれば解けます」まで言える。

法令由来の制約（`Rules.statutory`）には仮定を付けない。緩める候補として出したくないので。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from ortools.sat.python import cp_model

from .model import SLOT_BY_KEY, SLOTS, Assignment, Role, Schedule, Shop, Wish

# 希望が通らなかったときのコスト。数字の大小がそのまま優先順位になる。
COST_UNMET_WANT = 10  # 「入りたい」を落とす
COST_WRONG_ROLE = 3  # 入れたが、希望と違う持ち場だった
COST_FORCED_AVOID = 6  # 「できれば避けたい」に入れる
COST_UNDER_MIN_HOURS = 3  # 契約の下限時間に届かない（1時間あたり）
COST_UNFAIR = 1  # 人による総時間の偏り（1時間あたり）
COST_AGAINST_LOAD = 4  # 「控えめに」と言っている人に入れる（1時間あたり）
BONUS_WITH_LOAD = 2  # 「もっと入りたい」と言っている人に入れない（1時間あたり）


@dataclass
class Relaxable:
    """緩められる制約。解けなかったときの説明に使う。"""

    key: str
    label: str
    literal: cp_model.IntVar


@dataclass
class SolveResult:
    schedule: Schedule | None
    feasible: bool
    conflicts: list[Relaxable] = field(default_factory=list)
    status: str = ""
    wall_time_sec: float = 0.0
    timed_out: bool = False  # 時間内に判断できなかった（組めないのとは別）

    @property
    def undecided(self) -> bool:
        """組めるかどうかが分からないまま終わったか。"""
        return self.timed_out and not self.feasible


class ShiftSolver:
    def __init__(self, shop: Shop, *, time_limit_sec: float = 20.0) -> None:
        self.shop = shop
        self.time_limit_sec = time_limit_sec
        self.model = cp_model.CpModel()
        self.x: dict[tuple[str, date, str, Role], cp_model.IntVar] = {}
        self.relaxables: list[Relaxable] = []
        self._build()

    # ------------------------------------------------------------ 組み立て

    def _build(self) -> None:
        shop = self.shop
        rules = shop.rules

        # x[人, 日, コマ, 持ち場] = その人がそこに入るなら 1
        for s in shop.staff:
            for day in shop.dates:
                for slot in SLOTS:
                    for role in s.roles:
                        self.x[(s.id, day, slot.key, role)] = self.model.NewBoolVar(
                            f"x_{s.id}_{day:%m%d}_{slot.key}_{role.value}"
                        )

        self._c_one_role_per_slot()
        self._c_slots_per_day()
        self._c_demand()
        self._c_impossible()
        self._c_forced()
        self._c_days_in_a_row()
        self._c_weekly_hours()
        self._c_rest_between_days()
        self._c_veteran()
        self._c_labor_cost()
        self._c_min_hours()
        self._objective()

    def _works(self, staff_id: str, day: date, slot_key: str):
        """その人がその日そのコマに入るか（持ち場を問わない）。"""
        return [
            v
            for (sid, d, sk, _role), v in self.x.items()
            if sid == staff_id and d == day and sk == slot_key
        ]

    def _c_one_role_per_slot(self) -> None:
        """同じコマで2つの持ち場は持てない。"""
        for s in self.shop.staff:
            for day in self.shop.dates:
                for slot in SLOTS:
                    self.model.AddAtMostOne(self._works(s.id, day, slot.key))

    def _c_slots_per_day(self) -> None:
        """1日に入れるコマ数の上限。通し勤務を作らない。"""
        limit = self.shop.rules.max_slots_per_day
        for s in self.shop.staff:
            for day in self.shop.dates:
                worked = [v for slot in SLOTS for v in self._works(s.id, day, slot.key)]
                self.model.Add(sum(worked) <= limit)

    def _c_demand(self) -> None:
        """各コマの最低人数。ここは緩められる制約として扱う。

        店の判断で「この日は1人でも回す」と決められる性質のものなので、
        解けなかったときに緩める候補として出す。
        """
        for demand in self.shop.demands:
            for role, need in demand.required.items():
                if need <= 0:
                    continue
                assigned = [
                    v
                    for (sid, d, sk, r), v in self.x.items()
                    if d == demand.day and sk == demand.slot_key and r == role
                ]
                lit = self.model.NewBoolVar(f"relax_demand_{demand.day:%m%d}_{demand.slot_key}_{role.value}")
                self.model.Add(sum(assigned) >= need).OnlyEnforceIf(lit)
                self.relaxables.append(
                    Relaxable(
                        key=f"demand:{demand.day.isoformat()}:{demand.slot_key}:{role.value}",
                        label=f"{demand.day:%m/%d}（{SLOT_BY_KEY[demand.slot_key].label}）の"
                        f"{role.value}を{need}人以上にする",
                        literal=lit,
                    )
                )

    def _c_impossible(self) -> None:
        """本人が「不可」と出した日には入れない。

        希望の中でこれだけは守らなければならない側に置く。
        用事がある日に入れたシフトは、出しても出勤されない。
        ただし緩める候補には出す。本人に確認して覆ることはあるので。
        """
        for req in self.shop.requests:
            if req.wish is not Wish.IMPOSSIBLE:
                continue
            works = self._works(req.staff_id, req.day, req.slot_key)
            if not works:
                continue
            lit = self.model.NewBoolVar(f"relax_ng_{req.staff_id}_{req.day:%m%d}_{req.slot_key}")
            self.model.Add(sum(works) == 0).OnlyEnforceIf(lit)
            name = self.shop.staff_by_id(req.staff_id).name
            self.relaxables.append(
                Relaxable(
                    key=f"ng:{req.staff_id}:{req.day.isoformat()}:{req.slot_key}",
                    label=f"{name}さんの {req.day:%m/%d}（{SLOT_BY_KEY[req.slot_key].label}）不可を守る",
                    literal=lit,
                )
            )

    def _c_forced(self) -> None:
        """「この希望を必ず通したら何が起きるか」を確かめるための固定。

        説明を作るときだけ使う（explain.py）。通常の組み立てでは1件も入らない。
        """
        for req in self.shop.requests:
            if not req.forced:
                continue
            works = self._works(req.staff_id, req.day, req.slot_key)
            if works:
                self.model.Add(sum(works) == 1)

    def _c_days_in_a_row(self) -> None:
        """連続勤務日数の上限。法定側なので緩める候補にしない。"""
        rules = self.shop.rules
        dates = self.shop.dates
        for s in self.shop.staff:
            limit = min(rules.max_days_in_a_row, s.max_days_in_a_row)
            worked_on = {}
            for day in dates:
                w = self.model.NewBoolVar(f"w_{s.id}_{day:%m%d}")
                works = [v for slot in SLOTS for v in self._works(s.id, day, slot.key)]
                self.model.AddMaxEquality(w, works)
                worked_on[day] = w
            # 連続 limit+1 日の窓の中で、休みが1日以上あること
            for i in range(len(dates) - limit):
                window = [worked_on[d] for d in dates[i : i + limit + 1]]
                self.model.Add(sum(window) <= limit)

    def _c_weekly_hours(self) -> None:
        """週の上限時間。本人の契約による。"""
        for s in self.shop.staff:
            total = []
            for (sid, _d, sk, _r), v in self.x.items():
                if sid == s.id:
                    total.append(v * SLOT_BY_KEY[sk].hours)
            self.model.Add(sum(total) <= s.max_hours_per_week)

    def _c_rest_between_days(self) -> None:
        """勤務間インターバル。遅番の翌日に早番を入れない。"""
        need = self.shop.rules.min_rest_hours
        dates = self.shop.dates
        for s in self.shop.staff:
            for i in range(len(dates) - 1):
                today, tomorrow = dates[i], dates[i + 1]
                for a in SLOTS:
                    for b in SLOTS:
                        rest = (24 - a.end_hour) + b.start_hour
                        if rest >= need:
                            continue
                        for va in self._works(s.id, today, a.key):
                            for vb in self._works(s.id, tomorrow, b.key):
                                self.model.Add(va + vb <= 1)

    def _c_veteran(self) -> None:
        """新人だけのコマを作らない。店のルールなので緩める候補にする。"""
        if not self.shop.rules.veteran_required_per_slot:
            return
        veterans = [s.id for s in self.shop.staff if s.is_veteran]
        if not veterans:
            return
        for demand in self.shop.demands:
            if demand.total <= 0:
                continue
            on_duty = [
                v
                for (sid, d, sk, _r), v in self.x.items()
                if d == demand.day and sk == demand.slot_key and sid in veterans
            ]
            if not on_duty:
                continue
            lit = self.model.NewBoolVar(f"relax_vet_{demand.day:%m%d}_{demand.slot_key}")
            self.model.Add(sum(on_duty) >= 1).OnlyEnforceIf(lit)
            self.relaxables.append(
                Relaxable(
                    key=f"veteran:{demand.day.isoformat()}:{demand.slot_key}",
                    label=f"{demand.day:%m/%d}（{SLOT_BY_KEY[demand.slot_key].label}）に"
                    f"経験者を1人以上入れる",
                    literal=lit,
                )
            )

    def _c_labor_cost(self) -> None:
        """人件費の上限。予算なので緩める候補にする。"""
        limit = self.shop.rules.labor_cost_limit_per_week
        if limit is None:
            return
        cost = []
        for (sid, _d, sk, _r), v in self.x.items():
            wage = self.shop.staff_by_id(sid).hourly_wage
            cost.append(v * wage * SLOT_BY_KEY[sk].hours)
        lit = self.model.NewBoolVar("relax_cost")
        self.model.Add(sum(cost) <= limit).OnlyEnforceIf(lit)
        self.relaxables.append(
            Relaxable(key="cost", label=f"人件費を{limit:,}円以内に収める", literal=lit)
        )

    def _c_min_hours(self) -> None:
        """契約の最低時間。**必ず守る側に置く。**

        ここを目的関数の罰則だけにしていると、届かないシフトが「制約を満たした解」
        として返る。検査のほうは違反として数えるので、解けたと言いながら
        検査を通らないシフトが出てくることになり、「満たせないなら返さない」が
        成り立たない。

        ただし店が人を増やさないと物理的に届かないこともあるので、
        緩める候補には出す。外すかどうかは店長が決める。
        """
        for st in self.shop.staff:
            if st.min_hours_per_week <= 0:
                continue
            hours = sum(
                v * SLOT_BY_KEY[sk].hours
                for (sid, _d, sk, _r), v in self.x.items()
                if sid == st.id
            )
            lit = self.model.NewBoolVar(f"relax_minh_{st.id}")
            self.model.Add(hours >= st.min_hours_per_week).OnlyEnforceIf(lit)
            self.relaxables.append(
                Relaxable(
                    key=f"minhours:{st.id}",
                    label=f"{st.name}さんに契約の最低 {st.min_hours_per_week}時間を渡す",
                    literal=lit,
                )
            )

    def _objective(self) -> None:
        """通したい希望をできるだけ通す。ここは満たせなくても解は返る。"""
        terms = []

        for req in self.shop.requests:
            works = self._works(req.staff_id, req.day, req.slot_key)
            if not works:
                continue
            # 予定を守ってきた人の希望を重く見る。差は 0.6〜1.3 倍に収めてあるので、
            # 信頼が低い人の希望が無視されることはない
            w = self.shop.staff_by_id(req.staff_id).wish_weight
            if req.wish is Wish.WANT:
                # 入りたいのに入れなかったら加算
                miss = self.model.NewBoolVar(f"miss_{req.staff_id}_{req.day:%m%d}_{req.slot_key}")
                self.model.Add(sum(works) == 0).OnlyEnforceIf(miss)
                self.model.Add(sum(works) >= 1).OnlyEnforceIf(miss.Not())
                terms.append(miss * int(COST_UNMET_WANT * w * 10))

                # 持ち場まで指定していたら、違う持ち場に入れたぶんにも軽く加算する。
                # 指定を強く効かせると、ホール希望が集中したときにキッチンが埋まらない
                if req.role is not None:
                    wrong = [
                        v
                        for (sid, d, sk, r), v in self.x.items()
                        if sid == req.staff_id
                        and d == req.day
                        and sk == req.slot_key
                        and r != req.role
                    ]
                    if wrong:
                        terms.append(sum(wrong) * int(COST_WRONG_ROLE * w * 10))
            elif req.wish is Wish.AVOID:
                terms.append(sum(works) * int(COST_FORCED_AVOID * w * 10))

        # 契約の下限時間に届かない分。下限そのものは _c_min_hours で必ず守る。
        # ここは、下限を外して解いたときにも不足を小さく保つために残す
        for s in self.shop.staff:
            if s.min_hours_per_week <= 0:
                continue
            hours = sum(
                v * SLOT_BY_KEY[sk].hours
                for (sid, _d, sk, _r), v in self.x.items()
                if sid == s.id
            )
            shortfall = self.model.NewIntVar(0, s.min_hours_per_week, f"short_{s.id}")
            self.model.Add(shortfall >= s.min_hours_per_week - hours)
            terms.append(shortfall * COST_UNDER_MIN_HOURS * 10)

        # 日付の付かない意思表示。総量の側で効かせる
        for s in self.shop.staff:
            level = self.shop.load_level(s.id)
            if level == "normal":
                continue
            hours = sum(
                v * SLOT_BY_KEY[sk].hours
                for (sid, _d, sk, _r), v in self.x.items()
                if sid == s.id
            )
            if level == "lighter":
                # 下限は契約なので割れない。下限を超えたぶんにだけコストを付ける
                over = self.model.NewIntVar(0, s.max_hours_per_week, f"over_{s.id}")
                self.model.Add(over >= hours - s.min_hours_per_week)
                terms.append(over * COST_AGAINST_LOAD * 10)
            elif level == "more":
                short = self.model.NewIntVar(0, s.max_hours_per_week, f"want_more_{s.id}")
                self.model.Add(short >= s.max_hours_per_week - hours)
                terms.append(short * BONUS_WITH_LOAD * 10)

        # 人による総時間の偏り。最大と最小の差を詰める
        totals = []
        for s in self.shop.staff:
            t = self.model.NewIntVar(0, s.max_hours_per_week, f"total_{s.id}")
            self.model.Add(
                t
                == sum(
                    v * SLOT_BY_KEY[sk].hours
                    for (sid, _d, sk, _r), v in self.x.items()
                    if sid == s.id
                )
            )
            totals.append(t)
        if len(totals) >= 2:
            hi = self.model.NewIntVar(0, 200, "hours_max")
            lo = self.model.NewIntVar(0, 200, "hours_min")
            self.model.AddMaxEquality(hi, totals)
            self.model.AddMinEquality(lo, totals)
            gap = self.model.NewIntVar(0, 200, "hours_gap")
            self.model.Add(gap == hi - lo)
            terms.append(gap * COST_UNFAIR * 10)

        self.model.Minimize(sum(terms))

    # ------------------------------------------------------------ 解く

    def solve(self) -> SolveResult:
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.time_limit_sec
        solver.parameters.num_search_workers = 8

        # 緩められる制約は全部「守る」前提で解く。守れないなら、どれが原因かを聞き返す
        for r in self.relaxables:
            self.model.AddAssumption(r.literal)

        started = time.monotonic()
        status = solver.Solve(self.model)
        elapsed = time.monotonic() - started
        status_name = solver.StatusName(status)

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return SolveResult(
                schedule=self._extract(solver, status_name, elapsed),
                feasible=True,
                status=status_name,
                wall_time_sec=round(elapsed, 3),
            )

        # 時間切れは「組めない」ではない。まだ分かっていないだけ。
        # ここを一緒に扱うと、遅いだけの週に「組めません」と言ってしまう。
        # 現場では、組めない週と分からない週で打つ手がまったく違う
        if status != cp_model.INFEASIBLE:
            return SolveResult(
                schedule=None,
                feasible=False,
                status=status_name,
                wall_time_sec=round(elapsed, 3),
                timed_out=True,
            )

        # 解けなかった。どの仮定が同時に成り立たないかを受け取る
        conflict_indices = set(solver.SufficientAssumptionsForInfeasibility())
        by_index = {r.literal.Index(): r for r in self.relaxables}
        rough = [by_index[i] for i in conflict_indices if i in by_index]

        # CP-SAT が返すのは「十分な」集合であって最小ではない。
        # そのまま出すと100件を超えることがあり、「どれを緩めれば解けるか」の答えにならない。
        conflicts = self._minimize(rough)
        return SolveResult(
            schedule=None,
            feasible=False,
            conflicts=conflicts,
            status=status_name,
            wall_time_sec=round(time.monotonic() - started, 3),
        )

    def _infeasible_with(self, keep: list[Relaxable], solver: cp_model.CpSolver) -> bool:
        """指定した制約だけを課したときに、それでも解けないか。"""
        self.model.ClearAssumptions()
        for r in keep:
            self.model.AddAssumption(r.literal)
        return solver.Solve(self.model) == cp_model.INFEASIBLE

    def _minimize(self, conflicts: list[Relaxable], *, budget_sec: float = 10.0) -> list[Relaxable]:
        """矛盾集合を既約にする。

        1件ずつ外して解き直し、外しても解けないままならその制約は矛盾の原因ではない。
        最後まで残ったものだけが、同時には成り立たない組み合わせになる。

        制約1件につき1回解くので、元の集合が大きいと時間がかかる。
        打ち切ったときは既約になりきっていないことがあるので、そのまま返す。
        """
        if len(conflicts) <= 1:
            return conflicts

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 2.0
        solver.parameters.num_search_workers = 4

        keep = list(conflicts)
        deadline = time.monotonic() + budget_sec
        for candidate in list(conflicts):
            if time.monotonic() > deadline:
                break
            trial = [k for k in keep if k.key != candidate.key]
            if not trial:
                continue
            if self._infeasible_with(trial, solver):
                keep = trial  # 外しても解けない＝この制約は原因ではない
        return keep

    def _extract(self, solver: cp_model.CpSolver, status: str, elapsed: float) -> Schedule:
        assignments = []
        cost = 0
        for (sid, day, sk, role), v in self.x.items():
            if solver.Value(v):
                assignments.append(Assignment(staff_id=sid, day=day, slot_key=sk, role=role))
                cost += self.shop.staff_by_id(sid).hourly_wage * SLOT_BY_KEY[sk].hours

        assigned = {(a.staff_id, a.day, a.slot_key) for a in assignments}
        unmet = [
            r
            for r in self.shop.requests
            if r.wish is Wish.WANT and (r.staff_id, r.day, r.slot_key) not in assigned
        ]
        assignments.sort(key=lambda a: (a.day, a.slot_key, a.staff_id))
        return Schedule(
            assignments=assignments,
            unmet_wishes=unmet,
            labor_cost=cost,
            solver_status=status,
            wall_time_sec=round(elapsed, 3),
        )
