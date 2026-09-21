"""組めなかったときに、自分で手を打つ。

一度解いて終わりなら、それはただの計算で、エージェントとは呼べない。
現場の店長は、組めなかったらそこで止まらない。必要人数を見直したり、
誰かに相談したり、ルールを一時的に外したりして、組める形を探す。

ここではそれを回す。

    解く
      組めた   → 終わり
      組めない → 矛盾している制約を受け取る
                 ↓
                 緩められる候補を出す（法令は候補に出さない）
                 ↓
                 動かしたときの影響が小さい順に、実際に試す
                 ↓
                 組めたら、何を動かしたかを添えて人に返す
                 全部だめなら、試した記録を添えて人に返す

**勝手に確定はしない。** ルールを外して組めたシフトは、店長が外すと
決めない限り提案どまりにする。緩めてよいかを決めるのは人の側で、
どれを緩めれば組めるかを調べるのが機械の側、という分け方にしてある。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from .explain import group_conflicts
from .model import Role, Rules, Shop, Wish
from .solver import Relaxable, ShiftSolver, SolveResult

# 動かしやすい順。人に頼むのが一番あとに来る。
# 店が自分で決められることから試す
# 契約の最低時間を下げるのは、本人との約束を変える話なので最後。
# 店の基準を下げる → 入れない日を相談する → 契約を見直す、の順
PRIORITY = {"veteran": 0, "cost": 1, "demand": 2, "ng": 3, "minhours": 4}


@dataclass
class Step:
    """1回の試行。"""

    action: str  # 何をしたか
    detail: str
    feasible: bool
    conflicts: int = 0
    seconds: float = 0.0
    chosen_by: str = ""  # この一手を誰が選んだか（AI / 既定の順）
    reason: str = ""  # AI が選んだ理由。**まだ確かめていない言い分**


@dataclass
class AgentResult:
    result: SolveResult
    steps: list[Step] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)  # 組むために動かしたもの
    proposal: bool = False  # ルールを動かして組めた＝提案どまり

    @property
    def feasible(self) -> bool:
        return self.result.feasible


def _relax_label(r: Relaxable) -> str:
    kind = r.key.split(":")[0]
    if kind == "veteran":
        return f"{r.label} をやめる"
    if kind == "demand":
        return f"{r.label} を1人減らす"
    if kind == "cost":
        return "人件費の上限を外す"
    if kind == "ng":
        return f"{r.label} を外す（本人への確認が要る）"
    if kind == "minhours":
        return f"{r.label} をあきらめる（本人との契約の見直しが要る）"
    return r.label


def _apply(shop: Shop, r: Relaxable) -> Shop:
    """その制約を外した店を作る。元の店は触らない。"""
    kind, *rest = r.key.split(":")

    demands = [
        type(d)(day=d.day, slot_key=d.slot_key, required=dict(d.required))
        for d in shop.demands
    ]
    requests = list(shop.requests)
    rules = Rules(**{**shop.rules.__dict__})

    if kind == "veteran":
        # そのコマだけ外すことはできないので、店のルールごと落とす。
        # 影響が広いぶん、提案として人に見せる前提にしてある
        rules.veteran_required_per_slot = False
    elif kind == "cost":
        rules.labor_cost_limit_per_week = None
    elif kind == "demand" and len(rest) >= 3:
        day = date.fromisoformat(rest[0])
        slot_key, role_name = rest[1], rest[2]
        for d in demands:
            if d.day == day and d.slot_key == slot_key:
                for role in Role:
                    if role.value == role_name and d.required.get(role, 0) > 0:
                        d.required[role] -= 1
    elif kind == "minhours" and rest:
        # 約束した時間を渡せない、という提案になる。本人の合意が要るので
        # 提案どまりにするのは他と同じだが、優先順位は最後に置いてある
        staff = [
            replace(st, min_hours_per_week=0) if st.id == rest[0] else st
            for st in shop.staff
        ]
        return shop.with_changes(
            staff=staff, demands=demands, requests=requests, rules=rules
        )
    elif kind == "ng" and len(rest) >= 3:
        staff_id, day_iso, slot_key = rest[0], rest[1], rest[2]
        day = date.fromisoformat(day_iso)
        requests = [
            q
            for q in requests
            if not (
                q.staff_id == staff_id
                and q.day == day
                and q.slot_key == slot_key
                and q.wish is Wish.IMPOSSIBLE
            )
        ]

    return shop.with_changes(demands=demands, requests=requests, rules=rules)


def run(
    shop: Shop,
    *,
    time_limit_sec: float = 20.0,
    max_attempts: int = 6,
    on_step=None,
    advisor=None,
) -> AgentResult:
    """組めるまで手を打つ。打った手は全部記録に残す。

    `advisor` を渡すと、どの条件からゆずるかを AI が選ぶ。渡さないか、
    AI が答えられなかったときは、決め打ちの順（PRIORITY）で進む。
    どちらの場合も、選んだ手が効いたかどうかは解き直して確かめる。
    """
    first = ShiftSolver(shop, time_limit_sec=time_limit_sec).solve()
    steps = [
        Step(
            action="そのまま解く",
            detail="出された希望と店のルールをすべて満たす形を探す",
            feasible=first.feasible,
            conflicts=len(first.conflicts),
            seconds=first.wall_time_sec,
        )
    ]
    if on_step:
        on_step(steps[-1])

    if first.feasible:
        return AgentResult(result=first, steps=steps)

    # 時間切れのときに緩め始めるのが一番まずい。何が悪いのか分かっていないのに
    # 条件を外すことになる。組めない証拠がないうちは、手を打たずに人に返す
    if first.undecided:
        steps[-1] = Step(
            action="そのまま解く",
            detail="時間内に組めるかどうかを判断できなかった",
            feasible=False,
            conflicts=0,
            seconds=first.wall_time_sec,
        )
        return AgentResult(result=first, steps=steps)

    # 組めなかった。1つ外すたびに矛盾の中身は変わるので、
    # そのつど取り直して次の手を決める。最初のリストを使い続けると、
    # 外したあとに初めて出てくる問題（必要人数が足りない等）に手が届かない
    current = shop
    res = first
    applied: list[str] = []
    seen_once: set[str] = set()
    history: list[dict] = []  # 打った手とその結果。次の一手を決める材料にする
    stopped_by_ai = False

    for _ in range(max_attempts):
        if res.feasible:
            break

        # 同じ種類を繰り返しても代わり映えしないものは、候補から落とす
        usable = [
            c
            for c in res.conflicts
            if not (c.key.split(":")[0] in ("veteran", "cost")
                    and c.key.split(":")[0] in seen_once)
        ]
        if not usable:
            break

        # どれからゆずるかを AI に選ばせる。選べなければ既定の順に戻す。
        # 外の呼び出しが落ちても止まらないこと自体が、この作りの狙いでもある
        pick, chosen_by, reason = None, "既定の順", ""
        if advisor is not None:
            advice = advisor.choose(current, usable, history=history)
            if advice and advice.stop:
                # ゆずり続ければいつかは組める。それを組めたと言わないための判断。
                # 止めるのは安全側に倒れるので、そのまま受け入れる
                stopped_by_ai = True
                steps.append(
                    Step(
                        action="ここで止める",
                        detail="これ以上ゆずらず、店長に返すべきだと判断した",
                        feasible=False,
                        conflicts=len(res.conflicts),
                        chosen_by="AI",
                        reason=advice.reason,
                    )
                )
                if on_step:
                    on_step(steps[-1])
                break
            if advice and advice.key:
                pick = next((c for c in usable if c.key == advice.key), None)
                if pick is not None:
                    chosen_by, reason = "AI", advice.reason
            if pick is None and advice is not None and advice.error:
                reason = f"AIに聞けなかったので既定の順で進めた（{advice.error}）"

        if pick is None:
            pick = sorted(usable, key=lambda r: PRIORITY.get(r.key.split(":")[0], 9))[0]

        kind = pick.key.split(":")[0]
        if kind in ("veteran", "cost"):
            seen_once.add(kind)

        before = len(res.conflicts)
        current = _apply(current, pick)
        res = ShiftSolver(current, time_limit_sec=time_limit_sec).solve()
        applied.append(_relax_label(pick))
        history.append(
            {
                "action": _relax_label(pick),
                "before": before,
                "after": len(res.conflicts),
                "feasible": res.feasible,
            }
        )
        step = Step(
            action=_relax_label(pick),
            detail=(
                "この条件を外したが、時間内に判断できなかった"
                if res.undecided
                else "この条件を外して組み直した"
            ),
            feasible=res.feasible,
            conflicts=len(res.conflicts),
            seconds=res.wall_time_sec,
            chosen_by=chosen_by,
            reason=reason,
        )
        steps.append(step)
        if on_step:
            on_step(step)

    if res.feasible and not stopped_by_ai:
        return AgentResult(result=res, steps=steps, applied=applied, proposal=True)

    # 組めなかったときに返すのは、緩めたあとの店ではなく**元の店**の矛盾。
    # 店長は何も外すと決めていないので、実際に成り立っていないのは元の条件のほう。
    # 緩めた先の矛盾を見せると、外すと決めてもいない条件の話になる。
    # 途中で時間切れになった試行は steps に残るので、探索を打ち切ったことは追える
    return AgentResult(result=first, steps=steps, applied=applied)


def describe(shop: Shop, agent: AgentResult) -> str:
    """やったことを人に読める形にする。"""
    lines = ["エージェントがやったこと:"]
    for i, s in enumerate(agent.steps, 1):
        if s.feasible:
            mark = "組めた"
        elif "判断できなかった" in s.detail:
            mark = "時間内に判断できず"
        else:
            mark = f"組めない（矛盾 {s.conflicts}件）"
        who = f"［{s.chosen_by}］" if s.chosen_by else ""
        lines.append(f"  {i}. {who}{s.action} → {mark}  [{s.seconds:.2f}秒]")
        if s.reason:
            # AI の言い分。効いたかどうかは上の「→」が計算で確かめた結果
            lines.append(f"       AIの見立て: {s.reason}")

    if agent.feasible and agent.proposal:
        lines += [
            "",
            "そのままでは組めなかったので、次を外せば組めることを確かめました。",
        ]
        lines += [f"  - {a}" for a in agent.applied]
        if any(s.chosen_by == "AI" for s in agent.steps):
            lines += [
                "",
                "どれからゆずるかは AI が選びました。選んだ結果が本当に組めるかは、"
                "そのつど解き直して確かめています。",
            ]
        lines += ["", "外してよいかは店長が決めてください。これは提案で、確定ではありません。"]
    elif agent.result.undecided:
        lines += [
            "",
            "時間内に判断できませんでした。**組めないと分かったわけではありません。**",
            "制限時間を延ばすか、対象の週を短くして試してください。",
        ]
    elif any(s.action == "ここで止める" for s in agent.steps):
        lines += [
            "",
            "これ以上ゆずるべきでないと判断して止めました。同時に成り立たない条件:",
        ]
        lines += [f"  - {x}" for x in group_conflicts(agent.result.conflicts)]
    elif not agent.feasible:
        lines += ["", "試した範囲では組めませんでした。同時に成り立たない条件:"]
        lines += [f"  - {x}" for x in group_conflicts(agent.result.conflicts)]

    return "\n".join(lines)
