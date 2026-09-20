"""画面から出された希望と、記録された実績を、組み立てに流し込む。

画面で希望を出しても、次にシフトを組むときに読まなければ何も起きない。
信頼ポイントも同じで、記録しただけでは重みに乗らない。

ここが無いと、画面とソルバーが別々に動いているだけになる。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .keys import proposal_key
from .sanitize import detect_injection
from .confidence import read_confidence
from .model import SLOTS, LoadPreference, Request, Role, Shop, Wish

# グリッドで選んだ希望に付ける印。読み取りを通していないので、
# あとからモデルに投げ直してはいけない
GRID_NOTE = "画面から選択"
from .trust import DEFAULT_TRUST, load_events, scores

STATE_TO_WISH = {
    "want": Wish.WANT,
    "avoid": Wish.AVOID,
    "impossible": Wish.IMPOSSIBLE,
}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 書き込み途中で切れた行は捨てる
        # [] や null の行が混ざると、下で rec.get を呼んだところで落ちる。
        # 壊れた投稿1行でシフト作成全体が止まるのは割に合わない
        if isinstance(rec, dict):
            out.append(rec)
    return out


def needs_human_for(rec: dict) -> bool:
    """確認が要るかを、保存されている中身から計算し直す。

    `Proposal.needs_human` と同じ条件にしてある。片方だけ変えると、
    画面で確認待ちに見えているものが確認なしで通る、が起きる。

    **指示文の有無も記録を信じず、原文から見直す。** injections の行を
    消すだけで、指示文が混ざった希望が確認なしで通ってしまう。
    """
    kind = str(rec.get("kind", "unclear"))
    confidence = read_confidence(rec.get("confidence"))
    usable = bool(rec.get("days")) or str(rec.get("load", "normal")) != "normal"
    injected = detect_injection(str(rec.get("note", ""))) or rec.get("injections")
    return bool(
        injected
        or rec.get("error")
        or kind not in ("impossible", "avoid", "want")
        or not usable
        or confidence < 0.6
    )


def submission_key(rec: dict, staff_id: str) -> str:
    """確認済みかどうかを突き合わせる識別子を、中身から作り直す。

    **実際に適用される値を全部材料にする。** 文面だけを材料にすると、
    承認された文面はそのままに kind や days を書き換えて、別の制約を
    通せてしまう。承認は「この内容ちょうど」に対して出すものにする。

    人は表示名ではなく、在籍が確認できた staff_id で識別する。記録の名前を
    使うと、他人の承認済みレコードを写してきて自分の staff_id で適用する、が
    できてしまう。同姓同名がいる場合の取り違えも起きる。
    """
    return proposal_key(
        staff_id,
        str(rec.get("note", "")),
        "|".join(
            [
                str(rec.get("about", "")),
                str(rec.get("kind", "")),
                ",".join(sorted(str(d) for d in rec.get("days") or [])),
                ",".join(sorted(str(x) for x in rec.get("slots") or [])),
                str(rec.get("load", "normal")),
            ]
        ),
    )


def apply_submissions(
    shop: Shop, path: Path, *, decisions: dict | None = None
) -> tuple[int, int]:
    """画面から出された希望を取り込む。

    グリッドで選んだぶん（`picks`）は読み取りを通していないので、そのまま希望になる。
    自由文から読み取ったぶんは、確認が要るものを除いて取り込む。
    店長が確認待ちで「この内容で反映」を選んでいれば、それも取り込む。

    返り値は (取り込んだ希望の件数, 取り込んだ負荷の希望の人数)。
    """
    decisions = decisions or {}
    accepted_keys = {
        k for k, v in decisions.items() if v.get("action") in ("accept", "revise")
    }
    rejected_keys = {k for k, v in decisions.items() if v.get("action") == "reject"}

    valid_days = set(shop.dates)
    valid_staff = {s.id for s in shop.staff}
    # 保存されたファイルは書き換えられる。知らないコマがそのまま入ると、
    # あとで SLOT_BY_KEY を引いたところで落ちて、シフト作成ごと止まる
    valid_slots = {s.key for s in SLOTS}
    added = 0
    loads = 0
    seen_load: set[str] = set()

    for rec in _read_jsonl(path):
        if rec.get("week") != shop.start.isoformat():
            continue  # 別の週に出された希望
        staff_id = rec.get("staff_id")
        if staff_id not in valid_staff:
            continue

        # 1) グリッドで選んだぶん。読み取りを挟んでいないので確認は要らない
        for pick in rec.get("picks") or []:
            try:
                day = date.fromisoformat(pick["day"])
            except (KeyError, ValueError):
                continue
            wish = STATE_TO_WISH.get(pick.get("state", ""))
            slot_key = str(pick.get("slot", ""))
            if day not in valid_days or wish is None or slot_key not in valid_slots:
                continue
            role = None
            for r in Role:
                if r.value == pick.get("role"):
                    role = r
                    break
            shop.requests.append(
                Request(
                    staff_id=staff_id,
                    day=day,
                    slot_key=slot_key,
                    wish=wish,
                    note=GRID_NOTE,
                    role=role,
                )
            )
            added += 1

        # 2) 自由文から読み取ったぶん
        #
        # 保存されたファイルの判断をそのまま信じない。needs_human を false に
        # 書き換えるだけで、指示文が混ざったものや確信の低いものを確認なしで
        # 制約にできてしまう。承認済みの ack_key を写せば承認も流用できる。
        # どちらも中身から計算し直す
        # 人は表示名ではなく、在籍が確認できた staff_id で識別する
        key = submission_key(rec, staff_id)
        if key in rejected_keys:
            continue
        if needs_human_for(rec) and key not in accepted_keys:
            continue  # まだ確認が済んでいない

        for raw in rec.get("days") or []:
            try:
                day = date.fromisoformat(raw)
            except ValueError:
                continue
            if day not in valid_days:
                continue
            wish = STATE_TO_WISH.get(rec.get("kind", ""))
            if wish is None:
                continue
            slots = rec.get("slots") or sorted(valid_slots)
            for slot in slots:
                if slot not in valid_slots:
                    continue
                shop.requests.append(
                    Request(
                        staff_id=staff_id,
                        day=day,
                        slot_key=slot,
                        wish=wish,
                        note=rec.get("reason", ""),
                    )
                )
                added += 1

        level = rec.get("load", "normal")
        if level in ("lighter", "more") and staff_id not in seen_load:
            seen_load.add(staff_id)
            shop.load_preferences.append(
                LoadPreference(
                    staff_id=staff_id, level=level, reason=rec.get("reason", "")
                )
            )
            loads += 1

    return added, loads


def apply_trust(shop: Shop, path: Path) -> dict[str, int]:
    """記録された実績を、いまの信頼ポイントとして反映する。

    記録が無い人は、店側が持っている既定値のまま。
    """
    moved = scores(load_events(path))
    applied: dict[str, int] = {}
    for staff in shop.staff:
        if staff.id in moved:
            before = staff.trust
            staff.trust = moved[staff.id]
            if before != staff.trust:
                applied[staff.id] = staff.trust
    return applied
