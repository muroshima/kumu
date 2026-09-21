#!/usr/bin/env python3
"""働く人と店長が使う画面。

    uv run python scripts/build_shift.py --json runs/result.json
    uv run python scripts/serve.py

    /          自分のシフト（交代を頼める）
    /request   希望を出す
    /all       全員のシフト
    /review    店長: 確認待ち

依存は標準ライブラリだけ。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.parse
import webbrowser
from collections import defaultdict

WEEKDAY_LABEL = ("月", "火", "水", "木", "金", "土", "日")

# 希望欄の読み取りに使うモデル。build_shift.py と揃える
CHEAP_MODEL = os.environ.get("KUMU_MODEL", "orcarouter/zenken-cheap")
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kumu.inbox import needs_human_for, submission_key  # noqa: E402
from kumu.keys import ack_key, proposal_key  # noqa: E402

RESULT = ROOT / "runs" / "result.json"
IMPOSSIBLE = ROOT / "runs" / "impossible.json"
ACKED = ROOT / "runs" / "acked.json"
DECISIONS = ROOT / "runs" / "decisions.json"  # 確認待ちに対して店長が決めたこと
SUBMISSIONS = ROOT / "runs" / "submissions.jsonl"  # まだ組んでいない週に出された希望
TRUST_LOG = ROOT / "runs" / "trust.jsonl"  # 信頼ポイントの増減


def trust_of(staff_id: str) -> dict:
    """その人のいまの信頼ポイントと、直近の記録。"""
    from kumu.trust import DEFAULT_TRUST, history_of, load_events, scores

    events = load_events(TRUST_LOG)
    base = {}
    r = load(RESULT)
    for st in (r or {}).get("staff", []):
        base[st["id"]] = st.get("trust", DEFAULT_TRUST)
    moved = scores(events)
    score = moved.get(staff_id, base.get(staff_id, DEFAULT_TRUST))
    return {
        "score": score,
        "weight": round(0.6 + (score / 100) * 0.7, 2),
        "history": history_of(events, staff_id),
    }


# 募集を締め切ってから、その週が始まるまでに何日空けるか。
# 締切と週の頭を同じ日にすると、組み直しも交代の相談もできない。
LEAD_DAYS = 5


def confirmed_week() -> tuple[date, int]:
    """いま確定しているシフトの期間。"""
    r = load(RESULT)
    if not r:
        return date(2026, 10, 5), 7
    return date.fromisoformat(r["start"]), int(r["days"])


def open_week() -> tuple[date, int]:
    """いま希望を募集している期間。確定週の次の週。

    希望を出すのは、もう組み終わった週ではなく次の週。
    ここを取り違えると、出した希望がどこにも反映されない。
    """
    start, days = confirmed_week()
    return start + timedelta(days=days), days


def deadline_of(week_start: date) -> date:
    """その週の希望の締切。"""
    return week_start - timedelta(days=LEAD_DAYS)


def week_label(start: date, days: int) -> str:
    end = start + timedelta(days=days - 1)
    return (
        f"{start:%m/%d}（{WEEKDAY_LABEL[start.weekday()]}）"
        f"〜 {end:%m/%d}（{WEEKDAY_LABEL[end.weekday()]}）"
    )


def load_submissions() -> list[dict]:
    if not SUBMISSIONS.exists():
        return []
    out = []
    for line in SUBMISSIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_submission(record: dict) -> None:
    SUBMISSIONS.parent.mkdir(parents=True, exist_ok=True)
    with SUBMISSIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")



def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_acked() -> dict:
    return _load_json(ACKED)


def save_acked(data: dict) -> None:
    _save_json(ACKED, data)


def load_decisions() -> dict:
    return _load_json(DECISIONS)


def save_decisions(data: dict) -> None:
    _save_json(DECISIONS, data)

# 時間軸は0時から24時まで取る。深夜まで開けている店もあるので、
# 目盛りを営業時間に合わせてしまうと、その店でしか使えない画面になる。
HOUR_FROM, HOUR_TO = 0, 24
# この店が開けている時間。ここから外れたマスは選べないようにする
OPEN_FROM, OPEN_TO = 9, 23
SLOT_RANGE = {"early": (9, 15), "mid": (13, 19), "late": (17, 23)}
SLOT_COLOR = {"early": "a", "mid": "b", "late": "c"}

STYLE = """
:root{
  --bg:#f8fafc; --card:#fff; --ink:#1e293b; --ink-strong:#0f172a;
  --dim:#64748b; --faint:#94a3b8; --line:#e2e8f0; --soft:#f1f5f9;
  --brand-50:#eff6ff; --brand-100:#dbeafe; --brand-600:#2563eb; --brand-700:#1d4ed8;
  --warn-50:#fffbeb; --warn-600:#b45309; --warn-line:#fde68a;
  --alert-50:#fef2f2; --alert-600:#b91c1c; --alert-line:#fecaca;
  --ok-50:#f0fdf4; --ok-600:#15803d;
  --a:#60a5fa; --a-bg:#dbeafe; --b:#34d399; --b-bg:#d1fae5; --c:#a78bfa; --c-bg:#ede9fe;
  --shadow:0 1px 2px 0 rgb(0 0 0 / .05);
  --ease:cubic-bezier(.23,1,.32,1);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-size:15px;line-height:1.7;
  font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:860px;margin:0 auto;padding:0 20px 90px}

nav{background:var(--card);border-bottom:1px solid var(--line);margin-bottom:28px}
nav .inner{max-width:860px;margin:0 auto;padding:0 20px;display:flex;align-items:center;
  gap:2px;height:54px}
nav .brand{font-weight:700;color:var(--ink-strong);margin-right:16px;text-decoration:none}
nav a{padding:6px 12px;border-radius:8px;font-size:13.5px;color:var(--dim);text-decoration:none;
  transition:background-color 140ms var(--ease),color 140ms var(--ease)}
nav a[aria-current]{background:var(--brand-50);color:var(--brand-700);font-weight:600}
@media (hover:hover){nav a:hover{background:var(--soft)}}
nav form{margin-left:auto}
nav select{font:inherit;font-size:13px;padding:5px 9px;border:1px solid var(--line);
  border-radius:8px;background:var(--card);color:var(--ink)}

h1{font-size:20px;font-weight:700;color:var(--ink-strong);margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:13px;margin:0 0 22px}
h2{font-size:15px;font-weight:650;color:var(--ink-strong);margin:34px 0 10px}
h3.sec{font-size:12.5px;font-weight:600;color:var(--dim);margin:18px 0 8px;
  letter-spacing:.02em}

/* --- ガント --- */
.gantt{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:8px 16px 14px;box-shadow:var(--shadow);margin-bottom:10px;overflow-x:auto}
.ruler{display:grid;grid-template-columns:76px 1fr;align-items:end;
  padding-bottom:4px;border-bottom:1px solid var(--soft);margin-bottom:6px}
.ruler .ticks{display:grid;font-size:10px;color:var(--faint);font-variant-numeric:tabular-nums}
.ruler .ticks span{border-left:1px solid var(--line);padding-left:3px}
.row{display:grid;grid-template-columns:76px 1fr;align-items:center;gap:0;padding:3px 0}
.row .who{font-size:12.5px;color:var(--ink);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;padding-right:8px}
.row .who.vet::after{content:"";display:inline-block;width:5px;height:5px;border-radius:99px;
  background:var(--brand-600);margin-left:5px;vertical-align:2px}
.row .track{display:grid;position:relative;height:26px;align-items:center;min-width:520px}
.bar{border-radius:7px;height:24px;display:flex;align-items:center;padding:0 9px;
  font-size:11.5px;white-space:nowrap;overflow:hidden;color:var(--ink-strong)}
.bar.a{background:var(--a-bg)} .bar.b{background:var(--b-bg)} .bar.c{background:var(--c-bg)}
.bar .role{color:var(--dim);margin-left:5px;font-size:10.5px}
.warn-bar{margin:18px 0 0;background:var(--warn-50);border:1px solid var(--warn-line);
 border-radius:12px;padding:13px 16px;font-size:13px;color:var(--warn-600);line-height:1.6}
.warn-bar b{color:var(--ink-strong)}
.tried{margin-top:20px;background:var(--card);border:1px solid var(--line);
 border-radius:14px;padding:18px 20px}
.tried h3{font-size:14px;color:var(--ink-strong);margin:0 0 12px}
.tried ol{margin:0;padding-left:22px}
.tried li{margin:0 0 12px;font-size:13px;color:var(--ink);line-height:1.55}
.tried .by{display:inline-block;font-size:10.5px;font-weight:700;color:var(--brand-700);
 background:var(--brand-50);border:1px solid var(--brand-100);border-radius:5px;
 padding:1px 6px;margin-right:8px;vertical-align:1px}
.tried .act{margin-right:8px}
.tried .why{margin-top:5px;font-size:12px;color:var(--dim);line-height:1.6}
.tried .note{margin:12px 0 0;font-size:11.5px;color:var(--faint)}
.bar .act{margin-left:auto;font-size:10.5px;color:var(--brand-700);text-decoration:none;
  padding:1px 7px;border-radius:5px;background:rgb(255 255 255 / .6);
  transition:transform 140ms var(--ease)}
.bar .act:active{transform:scale(.95)}
.dayhead{display:flex;align-items:baseline;gap:8px;padding:4px 0 2px}
.dayhead .d{font-weight:700;font-variant-numeric:tabular-nums;color:var(--ink-strong);font-size:15px}
.dayhead .w{font-size:12px;color:var(--dim)}
.dayhead .w.we{color:var(--alert-600)}
.dayhead .short{margin-left:auto;font-size:11.5px;color:var(--warn-600)}

/* --- 自分のシフト --- */
.mine{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:16px 20px;margin-bottom:8px;box-shadow:var(--shadow);
  display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.mine .when{font-weight:650;color:var(--ink-strong);font-variant-numeric:tabular-nums}
.mine .w{font-size:12.5px;color:var(--dim)}
.mine .w.we{color:var(--alert-600)}
.mine .slot{font-size:13.5px;padding:3px 11px;border-radius:99px}
.mine .slot.a{background:var(--a-bg)} .mine .slot.b{background:var(--b-bg)}
.mine .slot.c{background:var(--c-bg)}
.mine .role{font-size:12.5px;color:var(--dim)}
.mine .btn{margin-left:auto}

.btn{display:inline-block;border-radius:9px;padding:8px 16px;font-weight:600;font-size:13.5px;
  text-decoration:none;border:1px solid var(--line);background:var(--card);color:var(--ink);
  cursor:pointer;transition:transform 140ms var(--ease),background-color 140ms var(--ease)}
.btn:active{transform:scale(.97)}
.btn.primary{background:var(--brand-600);color:#fff;border-color:transparent}
@media (hover:hover){.btn:hover{background:var(--soft)} .btn.primary:hover{background:var(--brand-700)}}

.item{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:18px 20px;margin-bottom:8px;box-shadow:var(--shadow)}
.item.alert{border-color:var(--alert-line);background:var(--alert-50)}
.item.ok{border-color:#bbf7d0;background:var(--ok-50)}
.item .top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.item .who{font-weight:650;color:var(--ink-strong)}
.item .when{color:var(--dim);font-size:13.5px}
.pill{margin-left:auto;font-size:11.5px;font-weight:600;padding:2px 10px;border-radius:99px}
.pill.no{background:var(--alert-50);color:var(--alert-600)}
.pill.yes{background:var(--ok-50);color:var(--ok-600)}
.pill.warn{background:var(--warn-50);color:var(--warn-600)}
.detail{margin-top:10px;font-size:13.5px}
.detail b{color:var(--ink-strong)}
.detail ul{margin:5px 0 0;padding-left:19px}
.detail li{padding:1px 0;color:var(--dim)}
.detail ul.fix li{color:var(--brand-700)}
.raw{margin-top:10px;padding:11px 13px;background:var(--soft);border-radius:8px;
  font-size:12.5px;white-space:pre-wrap;color:var(--dim);
  font-family:ui-monospace,Menlo,monospace;line-height:1.7}
.item.alert .raw{background:#fff}

.blocked{background:var(--alert-50);border:1px solid var(--alert-line);border-radius:14px;
  padding:22px 24px;margin:20px 0}
.blocked h3{font-size:17px;color:var(--alert-600);margin:0 0 10px}
.blocked ul{margin:0 0 16px;padding-left:20px}
.blocked li{padding:2px 0}
.blocked .fix{background:var(--card);border-radius:10px;padding:14px 16px}
.blocked .fix b{display:block;font-size:12px;color:var(--dim);margin-bottom:6px}
.blocked .fix li{color:var(--brand-700)}

form.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:22px;box-shadow:var(--shadow)}
label{display:block;font-size:13px;font-weight:600;color:var(--ink-strong);margin:16px 0 6px}
label:first-of-type{margin-top:0}
select,textarea{width:100%;font:inherit;font-size:14.5px;padding:10px 12px;
  border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink)}
textarea{min-height:100px;resize:vertical;line-height:1.7}
textarea:focus,select:focus{outline:2px solid var(--brand-100);outline-offset:1px;
  border-color:var(--brand-600)}
.hint{font-size:12px;color:var(--faint);margin-top:6px}

/* 希望を選ぶグリッド */
.pick{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:14px 18px 18px;box-shadow:var(--shadow);margin-bottom:14px;overflow-x:auto;
  user-select:none}
.modes{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.rolepick{margin:0 0 0 auto;font-size:12.5px;font-weight:500;color:var(--dim);
  display:flex;align-items:center;gap:7px}
.rolepick select{width:auto;font-size:12.5px;padding:5px 9px}
.myroles{font-size:13px;color:var(--dim);margin:0 0 10px}
.myroles b{color:var(--ink-strong)}
.mode{font:inherit;font-size:12.5px;padding:6px 14px;border-radius:99px;cursor:pointer;
  border:1px solid var(--line);background:var(--card);color:var(--dim);
  transition:transform 130ms var(--ease),border-color 130ms var(--ease),
    background-color 130ms var(--ease),color 130ms var(--ease)}
.mode:active{transform:scale(.97)}
.mode[aria-pressed="true"][data-m="want"]{background:#d1fae5;border-color:#6ee7b7;color:#065f46}
.mode[aria-pressed="true"][data-m="avoid"]{background:#fef3c7;border-color:#fcd34d;color:#92400e}
.mode[aria-pressed="true"][data-m="impossible"]{background:#fee2e2;border-color:#fca5a5;color:#991b1b}
.pick .prow{display:grid;grid-template-columns:72px 1fr;align-items:center;padding:2px 0}
.pick .pday{font-size:12.5px;color:var(--ink);font-variant-numeric:tabular-nums;
  white-space:nowrap;padding-right:8px}
.pick .pday em{font-style:normal;color:var(--dim);font-size:11px;margin-left:3px}
.pick .pday.we em{color:var(--alert-600)}
.pick .ptrack{position:relative;height:30px;display:grid;
  grid-template-rows:1fr;border-radius:7px;overflow:hidden;min-width:520px}
.pick{min-width:0}
.hr{grid-row:1;height:28px;cursor:crosshair;border:0;background:transparent;padding:0;
  border-right:1px solid var(--line)}
.hr:last-child{border-right:0}
.hr.closed{background:repeating-linear-gradient(45deg,var(--soft) 0,var(--soft) 3px,
  transparent 3px,transparent 6px);cursor:not-allowed}
.hr[data-on="want"]{background:#d1fae5}
.hr[data-on="avoid"]{background:#fef3c7}
.hr[data-on="impossible"]{background:#fee2e2}
.pick .slotmark{position:absolute;top:0;height:100%;pointer-events:none;
  border-left:1px dashed var(--line)}
.picked{grid-row:1;pointer-events:none;display:flex;align-items:center;justify-content:center;
  font-size:10.5px;font-weight:600;color:var(--ink-strong)}
.estimate{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  background:var(--brand-50);border-radius:10px;padding:13px 16px;margin-top:14px}
.estimate .amt{font-size:22px;font-weight:700;color:var(--brand-700);
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.estimate .l{font-size:12.5px;color:var(--dim)}
.estimate .warn{color:var(--warn-600);font-size:12px;margin-left:auto}
.empty{padding:44px 0;text-align:center;color:var(--faint);font-size:13.5px}
.trust{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:16px 20px;margin-bottom:14px;box-shadow:var(--shadow)}
.trust .tline{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.trust .tline b{font-size:15px;color:var(--ink-strong)}
.trust .tline .l{font-size:12.5px;color:var(--dim);margin-left:auto}
.trust .tbar{height:6px;border-radius:99px;background:var(--soft);margin-top:10px;
  overflow:hidden}
.trust .tbar span{display:block;height:100%;border-radius:99px;
  background:linear-gradient(90deg,var(--brand-600),#60a5fa)}
.trust .hint{margin-top:8px}
.item.acking{opacity:0;transform:translateY(-6px) scale(.99);
  transition:opacity 220ms var(--ease),transform 220ms var(--ease)}
.acts{display:flex;align-items:center;gap:8px;margin-top:14px;padding-top:12px;
  border-top:1px solid var(--soft)}
.ack{font:inherit;font-size:12.5px;font-weight:600;padding:6px 14px;border-radius:8px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;
  transition:transform 140ms var(--ease),background-color 140ms var(--ease)}
.ack:active{transform:scale(.97)}
@media (hover:hover){.ack:hover{background:var(--soft)}}
details.archive{margin-top:26px}
details.archive summary{cursor:pointer;font-size:13px;color:var(--dim);list-style:none;
  padding:10px 0}
details.archive summary::-webkit-details-marker{display:none}
details.archive summary::before{content:"▸ ";color:var(--faint)}
details.archive[open] summary::before{content:"▾ "}
.arch{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:12px 16px;margin-bottom:6px;display:flex;align-items:center;gap:12px;
  font-size:13px;color:var(--dim)}
.arch b{color:var(--ink);font-weight:600}
.arch .when{margin-left:auto;font-size:11.5px;color:var(--faint)}
.arch .undo{font-size:12px;color:var(--brand-700);background:none;border:0;cursor:pointer;
  font:inherit;padding:2px 6px}
.decided{margin-top:12px;padding:10px 13px;background:var(--brand-50);border-radius:9px;
  font-size:13px;color:var(--brand-700);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.decided b{font-weight:650}
.decided .when{margin-left:auto;font-size:11.5px;color:var(--faint)}
.decided .undo{font:inherit;font-size:12px;color:var(--brand-700);background:none;border:0;
  cursor:pointer;padding:2px 6px}
.rebuild{background:var(--warn-50);border:1px solid var(--warn-line);border-radius:12px;
  padding:14px 18px;margin:0 0 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  font-size:13.5px;color:var(--warn-600)}
.rebuild b{font-weight:650}
.rebuild .btn{margin-left:auto}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--dim);margin:0 0 14px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:4px;
  vertical-align:-1px}
footer{margin-top:36px;padding-top:18px;border-top:1px solid var(--line);
  text-align:center;font-size:11.5px;color:var(--faint)}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}
"""


def esc(x) -> str:
    return html.escape(str(x))


def load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def page(title: str, active: str, body: str, *, me: str = "", staff: list | None = None) -> str:
    def item(href: str, label: str, key: str) -> str:
        cur = ' aria-current="page"' if key == active else ""
        return f'<a href="{href}"{cur}>{label}</a>'

    picker = ""
    if staff:
        opts = "".join(
            f'<option value="{esc(s["id"])}"{" selected" if s["id"] == me else ""}>'
            f'{esc(s["name"])}</option>'
            for s in staff
        )
        picker = f"""<form method="get" action="/switch">
  <input type="hidden" name="to" value="{esc(active)}">
  <select name="me" onchange="this.form.submit()">{opts}</select></form>"""

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{STYLE}</style></head>
<body>
<nav><div class="inner">
  <a href="/" class="brand">kumu</a>
  {item("/", "自分のシフト", "mine")}
  {item("/request", "希望を出す", "request")}
  {item("/all", "全員のシフト", "all")}
  {item("/review", "確認待ち", "review")}
  {picker}
</div></nav>
<div class="wrap">{body}
<footer>架空の店とスタッフで動かしています。実在のデータは使っていません。</footer>
</div></body></html>"""


RULER_STEP = 3


def ruler() -> str:
    """時間の目盛り。24時間ぶんを1時間刻みで出すと読めないので3時間ごと。"""
    ticks = "".join(f"<span>{h}</span>" for h in range(HOUR_FROM, HOUR_TO, RULER_STEP))
    cols = (HOUR_TO - HOUR_FROM) // RULER_STEP
    return (
        f'<div class="ruler"><span></span>'
        f'<div class="ticks" style="grid-template-columns:repeat({cols},1fr)">{ticks}</div></div>'
    )


def bar(slot_key: str, label: str, role: str, action: str = "") -> str:
    start, end = SLOT_RANGE[slot_key]
    col_start = start - HOUR_FROM + 1
    col_span = end - start
    return (
        f'<div class="bar {SLOT_COLOR[slot_key]}" '
        f'style="grid-column:{col_start} / span {col_span}">'
        f'{esc(label)}<span class="role">{esc(role)}</span>{action}</div>'
    )


# ---------------------------------------------------------------- 自分のシフト


def render_mine(me: str) -> str:
    r = load(RESULT)
    if not r:
        return page("シフト", "mine", '<div class="empty">まだ組んでいません。</div>')

    staff = r.get("staff", [])
    name = next((s["name"] for s in staff if s["id"] == me), staff[0]["name"] if staff else "")

    rows = []
    for day in r.get("calendar", []):
        for slot in day["slots"]:
            for p in slot["assigned"]:
                if p["name"] != name:
                    continue
                we = " we" if day["weekday"] in ("土", "日") else ""
                rows.append(
                    f'<div class="mine">'
                    f'<span class="when">{esc(day["label"])}</span>'
                    f'<span class="w{we}">{esc(day["weekday"])}</span>'
                    f'<span class="slot {SLOT_COLOR[slot["key"]]}">{esc(slot["label"])} '
                    f'{esc(slot["hours"])}</span>'
                    f'<span class="role">{esc(p["role"])}</span>'
                    f'<a class="btn" href="/swap?me={esc(me)}&date={esc(day["date"])}'
                    f'&slot={esc(slot["key"])}">交代を頼む</a>'
                    f"</div>"
                )

    body = "".join(rows) or '<div class="empty">この期間の勤務はありません。</div>'
    total = len(rows)

    t = trust_of(me)
    bar_w = max(0, min(100, (t["score"] - 40) / 60 * 100))
    hist = ""
    if t["history"]:
        hist = "".join(
            f'<div class="arch"><b>{esc(h.note)}</b>'
            f'<span style="color:{"var(--ok-600)" if h.delta > 0 else "var(--alert-600)"}">'
            f'{h.delta:+d}</span>'
            f'<span class="when">{esc(h.at[:16].replace("T", " "))}</span></div>'
            for h in t["history"][:5]
        )
        hist = f'<details class="archive"><summary>最近の増減</summary>{hist}</details>'
    trust_block = (
        f'<div class="trust"><div class="tline"><b>信頼ポイント {t["score"]}</b>'
        f'<span class="l">希望の通りやすさ {t["weight"]}倍</span></div>'
        f'<div class="tbar"><span style="width:{bar_w:.0f}%"></span></div>'
        f'<p class="hint">予定どおり勤務すると上がり、当日の取り消しや無断欠勤で下がります。'
        f"契約した最低時間は、ポイントに関係なく割り当てます。</p>{hist}</div>"
    )
    return page(
        "自分のシフト",
        "mine",
        f'<h1>{esc(name)}さんのシフト</h1>'
        f'<p class="sub">{esc(week_label(date.fromisoformat(r["start"]), r["days"]))}'
        f' · {total}件</p>{trust_block}{body}',
        me=me,
        staff=staff,
    )


def try_log(r: dict) -> str:
    """組めなかったときに何を試したかを見せる。

    結果だけ出されても、何もせず諦めたのか、手を尽くしたのかが分からない。
    誰がその手を選んだのか（AI か、決め打ちの順か）も一緒に出す。
    AI の見立ては言い分なので、当たっていたかどうかは右の結果が示す。
    """
    steps = r.get("steps") or []
    if len(steps) <= 1:
        return ""
    rows = []
    for st in steps:
        if st.get("feasible"):
            mark = '<span class="pill yes">組めた</span>'
        elif st.get("action") == "ここで止める":
            mark = '<span class="pill">止めた</span>'
        else:
            mark = f'<span class="pill no">矛盾 {st.get("conflicts", 0)}件</span>'
        who = (
            f'<span class="by">{esc(st["chosen_by"])}</span>'
            if st.get("chosen_by")
            else ""
        )
        why = (
            f'<div class="why">{esc(st["reason"])}</div>' if st.get("reason") else ""
        )
        rows.append(
            f'<li>{who}<span class="act">{esc(st["action"])}</span>{mark}{why}</li>'
        )
    return (
        '<div class="tried"><h3>組めるようにするために試したこと</h3>'
        f'<ol>{"".join(rows)}</ol>'
        '<p class="note">どれをゆずるかは AI が選び、本当に組めるかは'
        'そのつど解き直して確かめています。</p></div>'
    )


# ---------------------------------------------------------------- 交代


def render_swap(me: str, day_iso: str, slot_key: str) -> str:
    from kumu.swap import find_substitute, schedule_from_report
    from kumu.workspace import shop_for_week

    r = load(RESULT)
    if not r:
        return page("交代", "mine", '<div class="empty">まだ組んでいません。</div>')

    # そのシフトを組んだときと同じ状態の店で解き直す。seed から作り直すと、
    # 取り込んだ希望も店長の確認結果も信頼ポイントも消えるので、
    # 元のシフトと違う制約で候補を出すことになる
    shop = shop_for_week(
        ROOT / "runs",
        start=date.fromisoformat(r["start"]),
        days=int(r["days"]),
    )
    schedule = schedule_from_report(shop, r["calendar"])
    d = date.fromisoformat(day_iso)
    res = find_substitute(shop, schedule, me, d, slot_key)

    staff = r.get("staff", [])
    name = next((s["name"] for s in staff if s["id"] == me), "")
    day = next(c for c in r["calendar"] if c["date"] == day_iso)
    slot = next(s for s in day["slots"] if s["key"] == slot_key)
    when = f'{day["label"]}（{day["weekday"]}）{slot["label"]}'

    if res.possible and res.substitutes:
        who = "・".join(res.substitutes)
        extra = ""
        if res.side_effects:
            extra = (
                f'<div class="detail">ほかに {esc("・".join(res.side_effects))} さんの'
                f"担当も動きます。</div>"
            )
        body = f"""
<div class="item ok">
  <div class="top"><span class="who">{esc(who)}さんなら代われます</span>
    <span class="pill yes">交代できる</span></div>
  <div class="detail">制約を確かめたうえでの候補です。連勤や勤務時間の上限には当たりません。</div>
  {extra}
  <div style="margin-top:14px"><a class="btn primary" href="/">依頼する</a>
    <a class="btn" href="/">やめる</a></div>
</div>"""
    elif res.undecided:
        body = """
<div class="item">
  <div class="top"><span class="who">まだ分かりません</span>
    <span class="pill">判断できず</span></div>
  <div class="detail">時間内に、代われる人がいるかどうかを判断できませんでした。
    代われないと分かったわけではありません。時間をおいてもう一度試してください。</div>
  <div style="margin-top:14px"><a class="btn" href="/">戻る</a></div>
</div>"""
    elif res.possible:
        body = """
<div class="item ok">
  <div class="top"><span class="who">代わりを立てずに回せます</span>
    <span class="pill yes">交代できる</span></div>
  <div class="detail">その日そのコマは、あなたが抜けても必要人数を満たしています。</div>
  <div style="margin-top:14px"><a class="btn primary" href="/">依頼する</a></div>
</div>"""
    else:
        from kumu.explain import group_conflicts, suggest_relaxations

        blockers = "".join(f"<li>{esc(b)}</li>" for b in group_conflicts(res.blockers))
        fixes = "".join(f"<li>{esc(s)}</li>" for s in suggest_relaxations(res.blockers, shop))
        body = f"""
<div class="item alert">
  <div class="top"><span class="who">代われる人がいません</span>
    <span class="pill no">交代できない</span></div>
  <div class="detail"><b>同時には成り立たない条件:</b><ul>{blockers}</ul></div>
  {f'<div class="detail">どれか1つを動かせば代われます:<ul class="fix">{fixes}</ul></div>' if fixes else ""}
  <div style="margin-top:14px"><a class="btn" href="/">戻る</a></div>
</div>"""

    return page(
        "交代を頼む",
        "mine",
        f'<h1>交代を頼む</h1><p class="sub">{esc(name)}さん · {esc(when)}</p>{body}',
        me=me,
        staff=staff,
    )


# ---------------------------------------------------------------- 全員のシフト


def render_all(me: str, path: Path = RESULT) -> str:
    r = load(path)
    if not r:
        return page("全員のシフト", "all", '<div class="empty">まだ組んでいません。</div>')
    staff = r.get("staff", [])

    if not r.get("feasible") and r.get("undecided"):
        # 組めないと分かったわけではない。ここを混ぜると、
        # 直さなくていいものを直しに行くことになる
        return page(
            "全員のシフト",
            "all",
            """<h1>この週はまだ分かりません</h1>
<p class="sub">時間内に、組めるかどうかを判断できませんでした。組めないと分かったわけではありません。</p>
<div class="blocked">
  <h3>できること</h3>
  <ul><li>時間をおいてもう一度組む</li>
      <li>対象の週を短くして試す</li></ul>
</div>""",
            me=me,
            staff=staff,
        )

    if not r.get("feasible"):
        conflicts = "".join(f"<li>{esc(c)}</li>" for c in r.get("conflicts", []))
        tried = try_log(r)
        fixes = "".join(f"<li>{esc(s)}</li>" for s in r.get("suggestions", []))
        return page(
            "全員のシフト",
            "all",
            f"""<h1>この週は組めません</h1>
<p class="sub">守れないシフトを出すより、出さない方を選びました。</p>
<div class="blocked">
  <h3>同時には成り立たない条件</h3>
  <ul>{conflicts}</ul>
  <div class="fix"><b>どれか1つを動かせば組めます</b>
    <ul class="fix" style="margin:0;padding-left:20px">{fixes}</ul></div>
</div>
{tried}
<p><a class="btn" href="/all">組めた週を見る</a></p>""",
            me=me,
            staff=staff,
        )

    days = []
    for day in r.get("calendar", []):
        lines = []
        for slot in day["slots"]:
            for p in slot["assigned"]:
                lines.append((p["name"], p["veteran"], slot["key"], slot["label"], p["role"]))
        # 人ごとに1行。同じ日に2コマは入らないので1人1本
        rows = "".join(
            f'<div class="row"><span class="who{" vet" if vet else ""}">{esc(nm)}</span>'
            f'<span class="track" style="grid-template-columns:repeat({HOUR_TO - HOUR_FROM},1fr)">'
            f"{bar(sk, lb, role)}</span></div>"
            for nm, vet, sk, lb, role in sorted(lines, key=lambda x: (SLOT_RANGE[x[2]][0], x[0]))
        )
        we = " we" if day["weekday"] in ("土", "日") else ""
        short = ""
        days.append(
            f'<div class="gantt"><div class="dayhead">'
            f'<span class="d">{esc(day["label"])}</span>'
            f'<span class="w{we}">{esc(day["weekday"])}</span>{short}</div>'
            f'{ruler()}{rows}</div>'
        )

    legend = """
<div class="legend">
  <span><i style="background:var(--a-bg)"></i>早番 9-15</span>
  <span><i style="background:var(--b-bg)"></i>中番 13-19</span>
  <span><i style="background:var(--c-bg)"></i>遅番 17-23</span>
  <span><i style="background:var(--brand-600);border-radius:99px;width:7px;height:7px"></i>経験者</span>
</div>"""

    link = (
        '<p style="margin-top:22px"><a class="btn" href="/all?week=impossible">'
        "人が足りない週を見る</a></p>"
        if IMPOSSIBLE.exists() and path is RESULT
        else ""
    )

    # 条件をゆずって組めた週は、そのことを先に言う。
    # 何ごともなく組めた週と同じ顔で出すと、ゆずった事実が伝わらない
    proposed = ""
    if r.get("proposal"):
        proposed = (
            '<div class="warn-bar">そのままでは組めなかったので、'
            "いくつかの条件をゆずれば組めることを確かめました。"
            "<b>ゆずってよいかは店長が決めてください。</b>これは提案で、確定ではありません。</div>"
        )

    return page(
        "全員のシフト",
        "all",
        f'<h1>全員のシフト</h1>'
        f'<p class="sub">{esc(week_label(date.fromisoformat(r["start"]), r["days"]))}</p>'
        f'{proposed}{try_log(r)}{legend}{"".join(days)}{link}',
        me=me,
        staff=staff,
    )


# ---------------------------------------------------------------- 希望を出す


def render_request(me: str, result: dict | None = None) -> str:
    r = load(RESULT)
    staff = (r or {}).get("staff", [])
    start, days = open_week()
    period = week_label(start, days)
    due = deadline_of(start)
    due_label = f"{due:%m/%d}（{WEEKDAY_LABEL[due.weekday()]}）"

    mine = [
        x
        for x in load_submissions()
        if x["staff_id"] == me and x["week"] == start.isoformat()
    ]
    submitted = ""
    if mine:

        def summarize(x: dict) -> str:
            """一覧に出す1行。補足を書かずグリッドだけで出すこともある。"""
            note = (x.get("note") or "").strip()
            if note:
                return note.splitlines()[0][:40]
            picks = x.get("picks") or []
            want = [p for p in picks if p.get("state") == "want"]
            if want:
                return f"時間帯を {len(picks)}件選択（入りたい {len(want)}件）"
            return f"時間帯を {len(picks)}件選択" if picks else "（内容なし）"

        rows = "".join(
            f'<div class="arch"><b>{esc(summarize(x))}</b>'
            f'<span class="when">{esc(x["at"][:16].replace("T", " "))}</span></div>'
            for x in reversed(mine[-5:])
        )
        submitted = f"<h2>この週に出した希望 <em>{len(mine)}件</em></h2>{rows}"

    out = render_request_result(result) if result else ""

    me_staff = next((x for x in staff if x["id"] == me), staff[0] if staff else {})
    wage = me_staff.get("hourly_wage", 1000)
    max_hours = me_staff.get("max_hours", 40)
    trust = trust_of(me)

    # 1時間刻みのマス。ドラッグで好きな長さの帯を引ける。
    # 早番/中番/遅番の3択だと「16時から21時まで」が出せない
    prows = []
    for i in range(days):
        d = start + timedelta(days=i)
        cells = []
        for h in range(HOUR_FROM, HOUR_TO):
            closed = not (OPEN_FROM <= h < OPEN_TO)
            cells.append(
                f'<button type="button" class="hr{" closed" if closed else ""}" '
                f'data-day="{d.isoformat()}" data-hour="{h}" data-on=""'
                + (" disabled" if closed else "")
                + f' style="grid-column:{h - HOUR_FROM + 1}"></button>'
            )
        hours = "".join(cells)
        we = " we" if d.weekday() >= 5 else ""
        prows.append(
            f'<div class="prow"><span class="pday{we}">{d:%m/%d}'
            f'<em>{WEEKDAY_LABEL[d.weekday()]}</em></span>'
            f'<span class="ptrack" data-day="{d.isoformat()}" '
            f'style="grid-template-columns:repeat({HOUR_TO - HOUR_FROM},1fr)">{hours}</span></div>'
        )
    my_roles = me_staff.get("roles", [])
    role_picker = ""
    if len(my_roles) > 1:
        opts = '<option value="">どこでもよい</option>' + "".join(
            f'<option value="{esc(r)}">{esc(r)}で入りたい</option>' for r in my_roles
        )
        role_picker = (
            '<label class="rolepick">持ち場<select id="role">' + opts + "</select></label>"
        )
    roles_line = (
        f'<p class="myroles">あなたが入れる持ち場：<b>{esc("・".join(my_roles))}</b>'
        + ("" if len(my_roles) > 1 else "（ここ以外には割り当てられません）")
        + "</p>"
    )

    ticks = "".join(f"<span>{h}</span>" for h in range(HOUR_FROM, HOUR_TO, RULER_STEP))
    grid = (
        roles_line
        + '<div class="pick">'
        '<div class="modes">'
        '<button type="button" class="mode" data-m="want" aria-pressed="true" '
        'onclick="setMode(this)">入りたい</button>'
        '<button type="button" class="mode" data-m="avoid" aria-pressed="false" '
        'onclick="setMode(this)">できれば避けたい</button>'
        '<button type="button" class="mode" data-m="impossible" aria-pressed="false" '
        'onclick="setMode(this)">入れない</button>'
        '<button type="button" class="mode" data-m="clear" aria-pressed="false" '
        'onclick="setMode(this)">消す</button>'
        + role_picker
        + '</div>'
        '<div class="ruler"><span></span>'
        f'<div class="ticks" style="grid-template-columns:repeat({(HOUR_TO - HOUR_FROM) // RULER_STEP},1fr)">'
        f'{ticks}</div></div>{"".join(prows)}'
        '<div class="estimate"><span class="amt" id="amt">0円</span>'
        '<span class="l" id="amtdetail">時間帯をドラッグで引いてください</span>'
        '<span class="warn" id="amtwarn"></span></div></div>'
        '<p class="hint" style="margin:-6px 0 14px">'
        '引いた時間は、重なっているコマ（早番 9-15 / 中番 13-19 / 遅番 17-23）の希望として扱います。'
        '</p>'
    )

    script = (
        "<script>\n"
        f"const WAGE={wage},MAX_HOURS={max_hours},H0={HOUR_FROM},H1={HOUR_TO};\n"
        "const SLOTS={early:[9,15],mid:[13,19],late:[17,23]};\n"
        "let mode='want',dragging=false,dragOn='';\n"
        "function setMode(b){mode=b.dataset.m;"
        "document.querySelectorAll('.mode').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));}\n"
        "function paint(el){el.dataset.on = (mode==='clear') ? '' : mode;}\n"
        "document.addEventListener('mousedown',e=>{if(!e.target.classList.contains('hr'))return;"
        "e.preventDefault();dragging=true;paint(e.target);update();});\n"
        "document.addEventListener('mouseover',e=>{if(dragging&&e.target.classList.contains('hr'))"
        "{paint(e.target);update();}});\n"
        "document.addEventListener('mouseup',()=>{dragging=false;});\n"
        "function update(){\n"
        " const on=Array.from(document.querySelectorAll('.hr')).filter(c=>c.dataset.on);\n"
        " const byDay={};\n"
        " on.forEach(c=>{(byDay[c.dataset.day]=byDay[c.dataset.day]||[]).push("
        "{h:Number(c.dataset.hour),s:c.dataset.on});});\n"
        " const picks=[];let wantHours=0;\n"
        " for(const day in byDay){\n"
        "  for(const key in SLOTS){const[a,b]=SLOTS[key];\n"
        "   const hit=byDay[day].filter(x=>x.h>=a&&x.h<b);\n"
        # 引いた時間が、そのコマの半分以上を覆っていたら、そのコマの希望とみなす
        "   if(hit.length*2 < (b-a)) continue;\n"
        "   const st=hit[0].s;const rl=(document.getElementById('role')||{value:''}).value;\n"
        "   picks.push(day+':'+key+':'+st+':'+rl);\n"
        "   if(st==='want') wantHours+=(b-a);}}\n"
        " const capped=Math.min(wantHours,MAX_HOURS);\n"
        " document.getElementById('amt').textContent=(capped*WAGE).toLocaleString()+'円';\n"
        " const n=picks.filter(p=>p.split(':')[2]==='want').length;\n"
        " document.getElementById('amtdetail').textContent=n\n"
        "   ? '入りたい '+n+'コマ・'+wantHours+'時間 × '+WAGE.toLocaleString()+'円'\n"
        "   : '時間帯をドラッグで引いてください';\n"
        " document.getElementById('amtwarn').textContent=wantHours>MAX_HOURS\n"
        "   ? '契約は週'+MAX_HOURS+'時間まで。'+(wantHours-MAX_HOURS)+'時間ぶんは入れません':'';\n"
        " document.getElementById('picks').value=picks.join(',');\n"
        "}\nupdate();\n</script>"
    )

    return page(
        "希望を出す",
        "request",
        f"""<h1>希望を出す</h1>
<p class="sub">{esc(period)} のシフト<br>
締切 {esc(due_label)} まで · 入りたいコマを選んで、足りないことは下に書いてください</p>

{grid}
<form class="card" method="post" action="/request">
  <input type="hidden" name="staff_id" value="{esc(me)}">
  <input type="hidden" name="picks" id="picks" value="">
  <label for="note">補足（任意）</label>
  <textarea id="note" name="note"
    placeholder="例：月末は他のバイトが入っているので全体的に控えめにしてほしいです"></textarea>
  <p class="hint">上のグリッドで選べないこと（事情や、全体の入り方の希望）があれば書いてください。</p>
  <div style="margin-top:18px"><button class="btn primary" type="submit">送信</button></div>
</form>
{script}
{out}{submitted}""",
        me=me,
        staff=staff,
    )


STATE_LABEL = {"want": "入りたい", "avoid": "できれば避けたい", "impossible": "入れない"}


def render_request_result(res: dict) -> str:
    p = res["proposal"]
    attacked = bool(p["injections"])

    picked = res.get("picks") or []
    grid_block = ""
    if picked:
        by_state: dict[str, list[str]] = defaultdict(list)
        for x in picked:
            d = date.fromisoformat(x["day"])
            label = {"early": "早番", "mid": "中番", "late": "遅番"}.get(x["slot"], x["slot"])
            role = f"／{x['role']}" if x.get("role") else ""
            by_state[x["state"]].append(
                f"{d:%m/%d}({WEEKDAY_LABEL[d.weekday()]}){label}{role}"
            )
        rows = "".join(
            f'<li><b>{esc(STATE_LABEL.get(st, st))}</b> — {esc("・".join(items))}</li>'
            for st, items in by_state.items()
        )
        grid_block = (
            f'<div class="item ok"><div class="top">'
            f'<span class="who">選んだ時間帯</span>'
            f'<span class="pill yes">そのまま反映します</span></div>'
            f'<div class="detail"><ul>{rows}</ul>'
            f"読み取りを通さないので、ここは確実に反映されます。</div></div>"
        )

    if attacked:
        kinds = "・".join(sorted({i["kind"] for i in p["injections"]}))
        pill = '<span class="pill no">反映しません</span>'
        detail = (
            f'<div class="detail"><b>指示文が混ざっています（{esc(kinds)}）。</b>'
            f"この内容はシフトに反映せず、店長の確認に回しました。<br>"
            f'読み取りは {esc(p["kind"])} / 確信度 {p["confidence"]:.1f} でしたが、'
            f"結果は変わりません。</div>"
        )
        cls = " alert"
    elif p["needs_human"]:
        pill = '<span class="pill warn">確認中</span>'
        detail = f'<div class="detail"><b>{esc(res["why"])}。</b>店長に確認しています。</div>'
        cls = ""
    else:
        days = "・".join(p["days"])
        slots = "・".join(p["slots"]) or "終日"
        pill = '<span class="pill yes">受け付けました</span>'
        detail = (
            f'<div class="detail"><b>{esc(p["reason"])}</b>'
            f"<ul><li>{esc(days)} の {esc(slots)}</li></ul></div>"
        )
        cls = " ok"

    if not p["note"].strip():
        return f"<h2>受け取りました</h2>{grid_block}"

    return f"""<h2>受け取りました</h2>
{grid_block}
<div class="item{cls}">
  <div class="top"><span class="who">{esc(p["staff_name"])}さん</span>{pill}</div>
  <div class="raw">{esc(p["note"])}</div>
  {detail}
</div>"""


# ---------------------------------------------------------------- 確認待ち（店長）


def render_review(me: str) -> str:
    r = load(RESULT)
    if not r:
        return page("確認待ち", "review", '<div class="empty">まだ組んでいません。</div>')
    staff = r.get("staff", [])
    acked = load_acked()

    decisions = load_decisions()

    def card(body: str, key: str, cls: str = "", *, actions: str = "") -> str:
        decided = decisions.get(key)
        state = ""
        if decided:
            label = {
                "accept": "この内容で反映します",
                "reject": "反映しません",
                "revise": "書き直しました",
            }.get(decided["action"], decided["action"])
            extra = f"：{esc(decided.get('note', ''))}" if decided.get("note") else ""
            state = (
                f'<div class="decided"><b>{esc(label)}</b>{extra}'
                f'<span class="when">{esc(decided.get("at", "")[:16].replace("T", " "))}</span>'
                f'<button class="undo" onclick="undecide(this)">取り消す</button></div>'
            )
        acts = actions if not decided else ""
        return (
            f'<div class="item{cls}" data-key="{esc(key)}">{body}{state}'
            f'<div class="acts">{acts}'
            f'<button class="ack" onclick="ack(this)">確認済みにする</button>'
            f"</div></div>"
        )

    def pending_actions(key: str, p: dict) -> str:
        """読み取れなかった希望に対して、店長が取れる手。

        確認するだけで何もできないなら、確認する意味がない。
        """
        buttons = ""
        if p.get("days") and not p.get("injections"):
            buttons += (
                f'<button class="ack" onclick="decide(this,\'accept\')">この内容で反映</button>'
            )
        buttons += (
            f'<button class="ack" onclick="decide(this,\'reject\')">反映しない</button>'
            f'<button class="ack" onclick="revise(this)">書き直す</button>'
        )
        return buttons

    # --- 確認待ちの希望
    pending_cards, archived = [], []
    for p in r.get("pending", []):
        key = proposal_key(p.get("staff_id", ""), p["note"], p.get("about", ""))
        is_attack = bool(p.get("injections"))
        pill = (
            '<span class="pill no">反映していません</span>'
            if is_attack
            else '<span class="pill warn">要確認</span>'
        )
        read = ""
        if p.get("days") or p.get("kind") not in (None, "unclear"):
            days = "・".join(p.get("days") or []) or "日付なし"
            slots = "・".join(p.get("slots") or []) or "終日"
            read = (
                f'<div class="detail">モデルの読み取り: <b>{esc(p.get("kind", ""))}</b>'
                f' / {esc(days)} / {esc(slots)} / 確信度 {p.get("confidence", 0):.1f}'
                + (f'<br>{esc(p["reason"])}' if p.get("reason") else "")
                + "</div>"
            )
        head = (
            f'<div class="top"><span class="who">{esc(p["staff_name"])}さん</span>'
            f'<span class="when">{esc(p["why"])}</span>{pill}</div>'
            f'<div class="raw">{esc(p["note"])}</div>{read}'
        )
        if key in acked:
            archived.append(
                (key, f'{p["staff_name"]}さん', p["why"], acked[key].get("at", ""))
            )
        else:
            pending_cards.append(
                (
                    is_attack,
                    card(
                        head,
                        key,
                        " alert" if is_attack else "",
                        actions=pending_actions(key, p),
                    ),
                )
            )

    attacked = [c for a, c in pending_cards if a]
    unread = [c for a, c in pending_cards if not a]

    # --- 通らなかった希望（人ごと）
    by_person: dict[str, list[dict]] = defaultdict(list)
    for u in r.get("unmet", []):
        by_person[u["staff_name"]].append(u)

    unmet_cards = []
    for name, items in sorted(by_person.items(), key=lambda kv: -len(kv[1])):
        key = ack_key("unmet", name, len(items))
        rows = []
        for u in items:
            when = f'{u["label"]}（{u["weekday"]}）{u["slot"]}'
            if u["satisfiable"]:
                others = "・".join(f'{n["name"]}さん' for n in u.get("newly_unmet", [])[:3])
                tail = f"{others}の希望と入れ替わりになります" if others else "組み直しで吸収できます"
            else:
                tail = (u.get("blockers") or ["条件が両立しません"])[0]
            rows.append(f"<li><b>{esc(when)}</b> — {esc(tail)}</li>")
        can = sum(1 for u in items if u["satisfiable"])
        pill = (
            f'<span class="pill warn">{len(items)}件</span>'
            if can
            else f'<span class="pill no">{len(items)}件</span>'
        )
        head = (
            f'<div class="top"><span class="who">{esc(name)}さん</span>'
            f'<span class="when">希望が通りませんでした</span>{pill}</div>'
            f'<div class="detail"><ul>{"".join(rows)}</ul></div>'
        )
        if key in acked:
            archived.append((key, f"{name}さん", f"希望が通らなかった {len(items)}件", acked[key].get("at", "")))
        else:
            unmet_cards.append(card(head, key))

    # --- 募集中の週に出された希望のうち、確認が要るもの
    open_start, open_days = open_week()
    open_end = open_start + timedelta(days=open_days - 1)
    open_cards = []
    for x in load_submissions():
        # 保存された needs_human ではなく、中身から計算し直す。
        # false に書き換えただけで確認待ちの画面から消せてしまう
        if x["week"] != open_start.isoformat() or not needs_human_for(x):
            continue
        # 取り込み側と同じ関数で作る。保存されている値を使うと、承認済みの
        # ものから写すだけで確認を通っていない希望に承認が効いてしまう
        # 在籍者一覧に無い id は表示しない。人は id で識別する
        sid = str(x.get("staff_id", ""))
        if not any(t["id"] == sid for t in staff):
            continue
        key = submission_key(x, sid)
        if key in acked:
            archived.append((key, f'{x["staff_name"]}さん', x["why"], acked[key].get("at", "")))
            continue
        is_attack = bool(x.get("injections"))
        pill = (
            '<span class="pill no">反映していません</span>'
            if is_attack
            else '<span class="pill warn">要確認</span>'
        )
        days_label = "・".join(x.get("days") or []) or "日付なし"
        read = (
            f'<div class="detail">モデルの読み取り: <b>{esc(x.get("kind", ""))}</b>'
            f' / {esc(days_label)} / 確信度 {x.get("confidence", 0):.1f}</div>'
        )
        head = (
            f'<div class="top"><span class="who">{esc(x["staff_name"])}さん</span>'
            f'<span class="when">{esc(x["why"])}</span>{pill}</div>'
            f'<div class="raw">{esc(x["note"])}</div>{read}'
        )
        open_cards.append(
            card(head, key, " alert" if is_attack else "", actions=pending_actions(key, x))
        )

    conf_start, conf_days = confirmed_week()
    conf_label = week_label(conf_start, conf_days)
    open_label = week_label(open_start, open_days)
    open_due = deadline_of(open_start)

    blocks = ""
    if open_cards:
        blocks += (
            f'<h2>{esc(open_label)} <em style="font-weight:400;color:var(--faint)">'
            f'募集中・締切 {open_due:%m/%d} · {len(open_cards)}件</em></h2>'
            f'{"".join(open_cards)}'
        )

    week_blocks = ""
    if attacked:
        week_blocks += f'<h3 class="sec">指示文が混ざっていたもの</h3>{"".join(attacked)}'
    if unread:
        week_blocks += f'<h3 class="sec">読み取れなかったもの</h3>{"".join(unread)}'
    if unmet_cards:
        week_blocks += f'<h3 class="sec">希望が通らなかった人</h3>{"".join(unmet_cards)}'
    if week_blocks:
        blocks += (
            f'<h2>{esc(conf_label)} <em style="font-weight:400;color:var(--faint)">'
            f'組み済み · {len(attacked) + len(unread) + len(unmet_cards)}件</em></h2>{week_blocks}'
        )

    if not blocks:
        blocks = '<div class="empty">確認が必要なものはありません。</div>'

    archive_html = ""
    if archived:
        rows = "".join(
            f'<div class="arch" data-key="{esc(k)}"><b>{esc(who)}</b>{esc(why)}'
            f'<span class="when">{esc(at[:16].replace("T", " "))}</span>'
            f'<button class="undo" onclick="unack(this)">戻す</button></div>'
            for k, who, why, at in archived
        )
        archive_html = (
            f'<details class="archive"><summary>確認済み {len(archived)} 件</summary>'
            f"{rows}</details>"
        )

    script = """<script>
async function post(url, body){
  await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)});
}
async function ack(btn){
  const card = btn.closest('.item');
  card.classList.add('acking');
  await post('/ack', {key: card.dataset.key,
    label: card.querySelector('.who').textContent,
    why: card.querySelector('.when').textContent});
  setTimeout(() => location.reload(), 220);
}
async function unack(btn){
  await post('/unack', {key: btn.closest('.arch').dataset.key});
  location.reload();
}
async function decide(btn, action){
  await post('/decide', {key: btn.closest('.item').dataset.key, action: action});
  location.reload();
}
async function undecide(btn){
  await post('/decide', {key: btn.closest('.item').dataset.key, action: ''});
  location.reload();
}
async function revise(btn){
  const card = btn.closest('.item');
  const current = card.querySelector('.raw').textContent.trim();
  const note = prompt('正しい内容を書いてください。この文章をもう一度読み取ります。', current);
  if(note === null || !note.trim()) return;
  btn.textContent = '読み取り中…';
  await post('/decide', {key: card.dataset.key, action: 'revise', note: note});
  location.reload();
}
</script>"""

    left = len(attacked) + len(unread) + len(unmet_cards) + len(open_cards)
    pending_changes = [d for d in decisions.values() if d.get("action") in ("accept", "revise")]
    rebuild = ""
    if pending_changes:
        rebuild = (
            f'<div class="rebuild"><b>{len(pending_changes)}件の修正が保留中です。</b>'
            f"シフトに反映するには組み直してください。"
            f'<a class="btn primary" href="/rebuild">組み直す</a></div>'
        )

    return page(
        "確認待ち",
        "review",
        f'<h1>確認待ち</h1><p class="sub">残り {left}件'
        + (f" · 確認済み {len(archived)}件" if archived else "")
        + f"</p>{rebuild}{blocks}{archive_html}{script}",
        me=me,
        staff=staff,
    )


def render_rebuild(me: str) -> str:
    """保留中の修正を反映してシフトを組み直す。

    ソルバーと説明の作り直しで1分近くかかるので、押した人にはそれを先に伝える。
    """
    import subprocess

    r = load(RESULT)
    staff = (r or {}).get("staff", [])
    decisions = load_decisions()
    changes = {k: v for k, v in decisions.items() if v.get("action") in ("accept", "revise")}

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_shift.py"), "--json", "runs/result.json"],
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=600,
    )
    ok = proc.returncode == 0

    if ok:
        # 反映が済んだ修正は決定から落とす。同じものを二度反映しない
        for k in changes:
            decisions.pop(k, None)
        save_decisions(decisions)

    tail = "\n".join(proc.stdout.strip().splitlines()[-6:]) or proc.stderr[-400:]
    body = (
        f'<div class="item ok"><div class="top"><span class="who">組み直しました</span>'
        f'<span class="pill yes">完了</span></div>'
        f'<div class="detail">{len(changes)}件の修正を反映しました。</div>'
        f'<div class="raw">{esc(tail)}</div>'
        f'<div style="margin-top:14px"><a class="btn primary" href="/all">シフトを見る</a>'
        f'<a class="btn" href="/review">確認待ちに戻る</a></div></div>'
        if ok
        else f'<div class="item alert"><div class="top"><span class="who">組み直せませんでした</span>'
        f'<span class="pill no">失敗</span></div><div class="raw">{esc(tail)}</div>'
        f'<div style="margin-top:14px"><a class="btn" href="/review">戻る</a></div></div>'
    )
    return page("組み直し", "review", f"<h1>組み直し</h1>{body}", me=me, staff=staff)


# ---------------------------------------------------------------- サーバ


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, code: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def _me(self, query: dict) -> str:
        if "me" in query:
            return query["me"][0]
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if part.strip().startswith("me="):
                return part.strip()[3:]
        r = load(RESULT)
        return (r or {}).get("staff", [{}])[0].get("id", "S01")

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        me = self._me(q)

        if u.path == "/switch":
            target = {"mine": "/", "request": "/request", "all": "/all", "review": "/review"}.get(
                q.get("to", ["mine"])[0], "/"
            )
            self.send_response(303)
            self.send_header("Set-Cookie", f"me={me}; Path=/")
            self.send_header("Location", target)
            self.end_headers()
            return

        if u.path in ("/", "/index.html"):
            self._send(render_mine(me))
        elif u.path == "/all":
            path = IMPOSSIBLE if q.get("week", [""])[0] == "impossible" else RESULT
            self._send(render_all(me, path))
        elif u.path == "/request":
            self._send(render_request(me))
        elif u.path == "/review":
            self._send(render_review(me))
        elif u.path == "/rebuild":
            self._send(render_rebuild(me))
        elif u.path == "/swap":
            self._send(render_swap(me, q.get("date", [""])[0], q.get("slot", [""])[0]))
        else:
            self.send_error(404)

    def _same_origin(self) -> bool:
        """このページ自身からの送信かどうか。

        画面のボタンは同一オリジンの fetch で application/json を送る。
        別のサイトに置いたフォームからは、その形を作れない。

        - `Content-Type` を application/json に限る。フォームが送れるのは
          urlencoded / multipart / text-plain の3つだけなので、これで
          プリフライトの要らない経路（enctype="text/plain" で本文を
          JSON の形に組む手口）を塞ぐ
        - `Origin` が付いていて自分自身と違うなら拒む。ブラウザは別オリジンへの
          POST に必ず Origin を付けるので、ここを抜けられない。
          curl などブラウザ以外からは Origin が付かないので、そこは通す

        ここを開けておくと、店長がサーバーを起動したまま別のサイトを開いた
        だけで、確認待ちを勝手に「この内容で反映」にできてしまう。
        モデルが騙されても最後は人が確認する、という前提が崩れる。
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.headers.get("Host") or ""
        return origin.split("//", 1)[-1] == host

    def do_POST(self):  # noqa: N802
        if not self._same_origin():
            self.send_error(403, "cross-site request")
            return
        if self.path == "/decide":
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_error(400)
                return
            key = payload.get("key", "")
            action = payload.get("action", "")
            decisions = load_decisions()
            if not action:
                decisions.pop(key, None)
            else:
                decisions[key] = {
                    "action": action,
                    "note": payload.get("note", ""),
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
            save_decisions(decisions)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path in ("/ack", "/unack"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_error(400)
                return
            key = payload.get("key", "")
            acked = load_acked()
            if self.path == "/ack" and key:
                acked[key] = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "label": payload.get("label", ""),
                    "why": payload.get("why", ""),
                }
            elif key:
                acked.pop(key, None)
            save_acked(acked)
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path != "/request":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        staff_id = form.get("staff_id", ["S01"])[0]
        note = form.get("note", [""])[0]
        picks = [x for x in form.get("picks", [""])[0].split(",") if x]

        from kumu.dummy import build
        from kumu.llm import LLM, Budget
        from kumu.report import _pending_reason
        from kumu.translate import Translator

        # 希望を出す先は、もう組み終わった週ではなく次の週
        start, days = open_week()
        shop = build(start=start, days=days)

        if note.strip():
            llm = LLM(
                budget=Budget(max_calls=5, max_tokens=50_000),
                cache_dir=ROOT / ".cache" / "llm",
            )
            p = Translator(llm, shop, model=CHEAP_MODEL).translate(
                staff_id, note
            )
        else:
            # グリッドだけで出したときは読み取るものがない。モデルを呼ばない
            from kumu.translate import Proposal

            p = Proposal(
                staff_id=staff_id,
                staff_name=shop.staff_by_id(staff_id).name,
                source_note="",
                kind="unclear",
            )

        # グリッドで選んだぶんは、そのまま希望になる。読み取りは要らない
        picked = []
        for raw in picks:
            parts = raw.split(":")
            if len(parts) >= 3:
                picked.append(
                    {
                        "day": parts[0],
                        "slot": parts[1],
                        "state": parts[2],
                        "role": parts[3] if len(parts) > 3 and parts[3] else "",
                    }
                )

        submitted_at = datetime.now().isoformat(timespec="seconds")
        append_submission(
            {
                "week": start.isoformat(),
                "picks": picked,
                # 確認待ちで下した決定と突き合わせるための識別子。
                # 画面側と同じ材料で作らないと、店長が承認しても突き合わない
                "ack_key": ack_key("submission", p.staff_name, note, submitted_at),
                "staff_id": staff_id,
                "staff_name": p.staff_name,
                "note": note,
                "kind": p.kind,
                "days": [d.isoformat() for d in p.days],
                "slots": p.slots,
                "load": p.load,
                "reason": p.reason,
                "confidence": p.confidence,
                "injections": p.injections,
                # 補足を書かずグリッドだけで出したなら、読み取るものがないので確認も要らない
                "needs_human": bool(note.strip()) and p.needs_human,
                "why": _pending_reason(p) if note.strip() else "",
                "at": submitted_at,
            }
        )

        self._send(
            render_request(
                staff_id,
                {
                    "proposal": {
                        "staff_name": p.staff_name,
                        "note": p.source_note,
                        "kind": p.kind,
                        "days": [d.isoformat() for d in p.days],
                        "slots": p.slots,
                        "reason": p.reason,
                        "confidence": p.confidence,
                        "injections": p.injections,
                        "needs_human": p.needs_human,
                    },
                    "why": _pending_reason(p),
                    "picks": picked,
                },
            )
        )

    def log_message(self, *args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    if not RESULT.exists():
        print("runs/result.json がありません。先に次を実行してください:")
        print("  uv run python scripts/build_shift.py --json runs/result.json")
        return 1

    url = f"http://localhost:{args.port}"
    print(f"{url}  （Ctrl+C で終了）")
    if not args.no_open:
        webbrowser.open(url)
    try:
        # 交代を探すときはソルバーを回すので、1リクエストで数秒止まる。
        # シングルスレッドだとその間ほかの画面が開けない。
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
