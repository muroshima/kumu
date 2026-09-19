"""画面から出された希望と、記録された実績を、組み立てに流し込む。

画面で希望を出しても、次にシフトを組むときに読まなければ何も起きない。
信頼ポイントも同じで、記録しただけでは重みに乗らない。

ここが無いと、画面とソルバーが別々に動いているだけになる。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .model import LoadPreference, Request, Role, Shop, Wish
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
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 書き込み途中で切れた行は捨てる
    return out


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
            if day not in valid_days or wish is None:
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
                    slot_key=pick.get("slot", ""),
                    wish=wish,
                    note="画面から選択",
                    role=role,
                )
            )
            added += 1

        # 2) 自由文から読み取ったぶん
        key = rec.get("ack_key") or ""
        if key in rejected_keys:
            continue
        if rec.get("needs_human") and key not in accepted_keys:
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
            slots = rec.get("slots") or ["early", "mid", "late"]
            for slot in slots:
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
