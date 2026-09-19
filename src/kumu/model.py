"""シフトを組むために必要なものの定義。

ここには「守らなければならないこと」と「できれば通したいこと」が混ざって出てくる。
その2つを最後まで混ぜないのが、このプログラムの方針になっている。

- **守らなければならないこと**（最低人数、連勤上限、法定の休憩）は、ソルバーの制約にする。
  満たせないなら解を返さない。「だいたい守れている」を出さない。
- **できれば通したいこと**（希望休、公平さ）は、違反にコストを付けて最小化する。
  通らなかったときは、なぜ通らなかったかを言えるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum


class Role(str, Enum):
    """その人が入れる持ち場。"""

    HALL = "ホール"
    KITCHEN = "キッチン"
    CASHIER = "レジ"


class Wish(str, Enum):
    """その日そのコマに対する本人の意思。"""

    WANT = "希望"  # 入りたい
    OK = "可"  # どちらでもよい
    AVOID = "できれば避けたい"  # 通らなくても働ける
    IMPOSSIBLE = "不可"  # 入れない（守らなければならないこと側に倒す）


@dataclass(frozen=True)
class Slot:
    """1日の中のコマ。"""

    key: str
    label: str
    start_hour: int
    end_hour: int

    @property
    def hours(self) -> int:
        return self.end_hour - self.start_hour


SLOTS = (
    Slot("early", "早番", 9, 15),
    Slot("mid", "中番", 13, 19),
    Slot("late", "遅番", 17, 23),
)
SLOT_BY_KEY = {s.key: s for s in SLOTS}


@dataclass
class Staff:
    """働く人。"""

    id: str
    name: str
    roles: list[Role]
    hourly_wage: int
    is_veteran: bool
    max_hours_per_week: int
    min_hours_per_week: int = 0
    max_days_in_a_row: int = 5  # 本人の契約上の上限。法定より厳しいことがある
    trust: int = 100  # 0-100。実績に応じて動く（→ trust.py）

    @property
    def wish_weight(self) -> float:
        """希望をどれだけ重く扱うか。

        入った予定を守ってきた人の希望を、当日に落とす人と同じ重さで扱うと、
        守っている側が損をする。ただし差を付けすぎると、一度遅刻した人が
        永久にシフトに入れなくなる。0.6〜1.3 の範囲に収める。
        """
        return round(0.6 + (self.trust / 100) * 0.7, 3)

    def can(self, role: Role) -> bool:
        return role in self.roles


@dataclass
class Demand:
    """その日そのコマに何人要るか。"""

    day: date
    slot_key: str
    required: dict[Role, int]  # 持ち場ごとの最低人数

    @property
    def total(self) -> int:
        return sum(self.required.values())


@dataclass
class Request:
    """本人から出てきた希望。

    `note` は本人が自由に書いた文章で、ここだけは形が決まっていない。
    「来週は子どもの運動会があるので土曜は無理です」のような書き方で来る。
    決まった形に落とすのはモデルの仕事（translate.py）。
    """

    staff_id: str
    day: date
    slot_key: str
    wish: Wish
    note: str = ""
    role: "Role | None" = None  # 「この時間はホールで入りたい」。指定しなければどこでもよい
    forced: bool = False  # 「本当に通せるのか」を確かめるとき、この1件だけを必須にする


@dataclass
class LoadPreference:
    """「できれば控えめに」「もっと入りたい」という、日付の付かない意思表示。

    「月末は他のバイトが入っているので厳しいです」は、どの日かを特定できなくても
    意思ははっきり読み取れる。日付が取れないからと捨てると、本人が書いたのに
    何も反映されないことになる。総量の側で効かせる。
    """

    staff_id: str
    level: str  # "lighter"（控えめに） / "more"（もっと） / "normal"
    reason: str = ""


@dataclass
class Rules:
    """店と法令が決めていること。

    労働基準法まわりの値はここに集約する。店の都合で緩められるものと、
    緩めてはいけないものを混ぜないために、`statutory` で印を付けている。
    """

    max_days_in_a_row: int = 6  # 法定。7日連続は休日が無いことになる
    max_hours_per_day: int = 8
    max_slots_per_day: int = 1
    min_rest_hours: int = 11  # 勤務間インターバル。遅番の翌日に早番を入れない根拠
    veteran_required_per_slot: bool = True  # 新人だけのコマを作らない
    labor_cost_limit_per_week: int | None = None

    statutory: frozenset[str] = frozenset(
        {"max_days_in_a_row", "max_hours_per_day", "min_rest_hours"}
    )

    def is_statutory(self, name: str) -> bool:
        """法令由来か。法令由来のものは「緩めれば解ける」の候補に出さない。"""
        return name in self.statutory


@dataclass
class Shop:
    """1店舗ぶんの入力一式。"""

    name: str
    start: date
    days: int
    staff: list[Staff]
    demands: list[Demand]
    requests: list[Request] = field(default_factory=list)
    rules: Rules = field(default_factory=Rules)
    load_preferences: list[LoadPreference] = field(default_factory=list)

    def load_level(self, staff_id: str) -> str:
        for lp in self.load_preferences:
            if lp.staff_id == staff_id:
                return lp.level
        return "normal"

    @property
    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]

    def staff_by_id(self, staff_id: str) -> Staff:
        for s in self.staff:
            if s.id == staff_id:
                return s
        raise KeyError(staff_id)

    def demand(self, day: date, slot_key: str) -> Demand | None:
        for d in self.demands:
            if d.day == day and d.slot_key == slot_key:
                return d
        return None

    def request(self, staff_id: str, day: date, slot_key: str) -> Request | None:
        for r in self.requests:
            if r.staff_id == staff_id and r.day == day and r.slot_key == slot_key:
                return r
        return None


@dataclass
class Assignment:
    """誰がいつどの持ち場に入るか、1件ぶん。"""

    staff_id: str
    day: date
    slot_key: str
    role: Role


@dataclass
class Schedule:
    """組み上がったシフト。"""

    assignments: list[Assignment]
    unmet_wishes: list[Request] = field(default_factory=list)
    labor_cost: int = 0
    solver_status: str = ""
    wall_time_sec: float = 0.0

    def for_day(self, day: date) -> list[Assignment]:
        return [a for a in self.assignments if a.day == day]

    def for_staff(self, staff_id: str) -> list[Assignment]:
        return sorted(
            (a for a in self.assignments if a.staff_id == staff_id),
            key=lambda a: (a.day, a.slot_key),
        )
