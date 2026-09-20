"""なぜそうなったかを答える。

シフトに納得できないとき、人が聞きたいのは「なぜ私の希望が通らなかったのか」で、
「AIが総合的に判断しました」では答えになっていない。

ここでは説明を文章として作らない。**ソルバーに問い直して、返ってきた事実を並べる**。

    希望が通らなかった → その希望を必ず通す条件で解き直す
        解けた   → 通せる。ただし代わりに何が起きるかを差分で見せる
        解けない → 同時には成り立たない制約の組み合わせを出す

言語モデルに書かせるのは、最後に日本語へ直すところだけにする。
理由そのものをモデルに考えさせると、それらしい文章は出るが、根拠が無い。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .model import SLOT_BY_KEY, Request, Schedule, Shop, Wish
from .solver import Relaxable, ShiftSolver


@dataclass
class Tradeoff:
    """希望を通した場合に、代わりに起きること。"""

    newly_unmet: list[Request] = field(default_factory=list)
    cost_delta: int = 0
    moved_staff: list[str] = field(default_factory=list)


@dataclass
class WishExplanation:
    """1件の希望について、通らなかった理由。"""

    request: Request
    satisfiable: bool  # 他を犠牲にすれば通せるか
    blockers: list[Relaxable] = field(default_factory=list)  # 通せない場合の原因
    tradeoff: Tradeoff | None = None  # 通せる場合の代償


def group_conflicts(conflicts: list[Relaxable]) -> list[str]:
    """矛盾集合を人に読める形にまとめる。

    「佐藤さんの10/03早番」「同 中番」「同 遅番」が並んでいても、
    読む側が知りたいのは「佐藤さんが10/03に終日入れないこと」なので、そこまで畳む。
    """
    ng_by_person_day: dict[tuple[str, str], list[str]] = defaultdict(list)
    others: list[str] = []

    for c in conflicts:
        parts = c.key.split(":")
        if parts[0] == "ng" and len(parts) == 4:
            _, staff_id, day_iso, slot_key = parts
            name = c.label.split("さんの")[0]
            ng_by_person_day[(name, day_iso)].append(slot_key)
        else:
            others.append(c.label)

    lines: list[str] = []
    for (name, day_iso), slots in sorted(ng_by_person_day.items()):
        d = date.fromisoformat(day_iso)
        if len(slots) >= 3:
            lines.append(f"{name}さんが {d:%m/%d} は終日入れない")
        else:
            labels = "・".join(SLOT_BY_KEY[s].label for s in sorted(slots))
            lines.append(f"{name}さんが {d:%m/%d} の {labels} に入れない")
    lines.extend(others)
    return lines


def suggest_relaxations(conflicts: list[Relaxable], shop: Shop) -> list[str]:
    """どれを動かせば解けるようになるかを出す。

    矛盾集合に入っている制約は、どれか1つでも外せば解ける可能性がある。
    現場で動かしやすい順に並べる。人に頼み直すより、店の基準を下げる方が早い。
    """
    by_kind: dict[str, list[Relaxable]] = defaultdict(list)
    for c in conflicts:
        by_kind[c.key.split(":")[0]].append(c)

    out: list[str] = []
    # 店が自分で決められるものから提案する
    for c in by_kind.get("veteran", []):
        out.append(f"{c.label} をやめる（そのコマは経験者なしで回す）")
    for c in by_kind.get("demand", []):
        out.append(f"{c.label} を1人減らす")
    if by_kind.get("cost"):
        out.append("人件費の上限を引き上げる")
    # 人に頼むのは最後
    ng = by_kind.get("ng", [])
    if ng:
        names = sorted({c.label.split("さんの")[0] for c in ng})
        out.append(f"{'・'.join(names)}さんに、その日に入れないか相談する")
    return out


class Explainer:
    def __init__(self, shop: Shop, schedule: Schedule, *, time_limit_sec: float = 10.0) -> None:
        self.shop = shop
        self.schedule = schedule
        self.time_limit_sec = time_limit_sec

    def why_not(self, request: Request) -> WishExplanation:
        """その希望が通らなかった理由を、解き直して確かめる。"""
        # 希望以外は元の店のまま解き直す。ここで条件が1つでも変わると、
        # 出てくるのは「その希望を通したときに起きること」ではなくなる。
        # 説明は事実だと言っている以上、違う店で解いた結果を出してはいけない
        forced = self.shop.with_changes(requests=self._with_forced(request))
        result = ShiftSolver(forced, time_limit_sec=self.time_limit_sec).solve()

        if not result.feasible or result.schedule is None:
            return WishExplanation(
                request=request, satisfiable=False, blockers=result.conflicts
            )

        return WishExplanation(
            request=request,
            satisfiable=True,
            tradeoff=self._diff(result.schedule),
        )

    def _with_forced(self, request: Request) -> list[Request]:
        """対象の希望を「必ず通す」側に置き換えた希望一覧を作る。

        入りたい希望なら、その人をそのコマに固定する。
        避けたい・不可なら、そのコマから外すのを必須にする。
        """
        out = [
            r
            for r in self.shop.requests
            if not (
                r.staff_id == request.staff_id
                and r.day == request.day
                and r.slot_key == request.slot_key
            )
        ]
        if request.wish is Wish.WANT:
            # 「入りたい」を必ず通す＝本人をそのコマに固定して解き直す
            out.append(
                Request(
                    staff_id=request.staff_id,
                    day=request.day,
                    slot_key=request.slot_key,
                    wish=Wish.WANT,
                    note=request.note,
                    forced=True,
                )
            )
        else:
            out.append(
                Request(
                    staff_id=request.staff_id,
                    day=request.day,
                    slot_key=request.slot_key,
                    wish=Wish.IMPOSSIBLE,
                    note=request.note,
                )
            )
        return out

    def _diff(self, other: Schedule) -> Tradeoff:
        """元のシフトと、希望を通したシフトの差を取る。"""
        before = {(a.staff_id, a.day, a.slot_key) for a in self.schedule.assignments}
        after = {(a.staff_id, a.day, a.slot_key) for a in other.assignments}

        before_unmet = {(r.staff_id, r.day, r.slot_key) for r in self.schedule.unmet_wishes}
        newly = [r for r in other.unmet_wishes if (r.staff_id, r.day, r.slot_key) not in before_unmet]

        moved = sorted(
            {
                self.shop.staff_by_id(sid).name
                for (sid, _d, _sk) in (before ^ after)
            }
        )
        return Tradeoff(
            newly_unmet=newly,
            cost_delta=other.labor_cost - self.schedule.labor_cost,
            moved_staff=moved,
        )


def render_wish_explanation(exp: WishExplanation, shop: Shop) -> str:
    """説明を日本語にする。数値と固有名詞はここで作らず、受け取ったものだけ使う。"""
    r = exp.request
    who = shop.staff_by_id(r.staff_id).name
    when = f"{r.day:%m/%d}（{SLOT_BY_KEY[r.slot_key].label}）"

    if not exp.satisfiable:
        lines = [f"{who}さんの {when} の希望は、どう組んでも通せません。", "", "同時には成り立たない条件:"]
        lines += [f"  - {line}" for line in group_conflicts(exp.blockers)]
        suggestions = suggest_relaxations(exp.blockers, shop)
        if suggestions:
            lines += ["", "どれか1つを動かせば通せます:"]
            lines += [f"  - {s}" for s in suggestions]
        return "\n".join(lines)

    t = exp.tradeoff
    lines = [f"{who}さんの {when} の希望は、通すこと自体はできます。", "", "ただし代わりにこうなります:"]
    if t and t.newly_unmet:
        for other in t.newly_unmet[:5]:
            other_who = shop.staff_by_id(other.staff_id).name
            other_when = f"{other.day:%m/%d}（{SLOT_BY_KEY[other.slot_key].label}）"
            lines.append(f"  - {other_who}さんの {other_when} の希望が通らなくなります")
    if t and t.cost_delta:
        sign = "増えます" if t.cost_delta > 0 else "減ります"
        lines.append(f"  - 人件費が {abs(t.cost_delta):,}円 {sign}")
    if t and not t.newly_unmet and not t.cost_delta:
        lines.append("  - 目立った影響はありません（組み直しで吸収できます）")
    return "\n".join(lines)
