#!/usr/bin/env node
// 登壇カンペの読み上げ字数を数える。
// 数える対象: 引用行（> で始まる＝実際に口に出す文）と、デモ表の「話すこと」列。
// 換算レート: 落ち着いて 268字/分 / 早口 330字/分
// デモは操作待ちがあるため字数ではなく実時間 3:00 で固定して足す。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(here, "..", "..", "docs", "pitch", "talk_script.md");
const CALM = 268 / 60;
const FAST = 330 / 60;
const DEMO_SEC = 180;
const BUDGET_SEC = 600;
const TOLERANCE_SEC = 10;

const strip = (s) => s.replace(/[>*（）()「」【】\s|]/g, "").length;
// 秒を先に丸めてから分秒に割る。剰余を個別に丸めると 419.5秒 が "6:60" になる。
const mmss = (sec) => {
  const t = Math.round(sec);
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
};

const lines = fs.readFileSync(SRC, "utf8").split("\n");

// セクション境界を見出しから求める（行番号を埋め込まない）
const idx = (re) => lines.findIndex((l) => re.test(l));
const extraStart = idx(/^# ⏱ 時間が余ったら/);
const qaStart = idx(/^# 想定Q&A/);
if (extraStart < 0 || qaStart < 0) throw new Error("見出しが見つかりません（talk_script.md の構成が変わった可能性）");

const count = (from, to) => lines.slice(from, to).filter((l) => l.startsWith(">")).reduce((n, l) => n + strip(l), 0);

// デモ表の「話すこと」列（| # | 操作 | 話すこと | 目安 | の3列目）
const demo = lines
  .filter((l) => /^\| [1-6] \| /.test(l))
  .reduce((n, l) => n + strip(l.split("|")[3] ?? ""), 0);

const core = count(0, extraStart);
const extra = count(extraStart, qaStart);

for (const [label, n] of [
  ["デモ以外の発話", core],
  ["（参考）デモ中の発話", demo],
  ["⏱ 時間が余ったら", extra],
]) {
  console.log(`${label.padEnd(22, "　")} ${String(n).padStart(5)}字   落ち着いて ${mmss(n / CALM)} / 早口 ${mmss(n / FAST)}`);
}

console.log("");
for (const [label, rate] of [["落ち着いて", CALM], ["早口", FAST]]) {
  const total = core / rate + DEMO_SEC;
  const diff = BUDGET_SEC - total;
  // 話速の推定そのものが数秒の幅を持つので、±TOLERANCE_SEC は誤差として扱う。
  // 1秒の超過を警告すると、実際には無い精度を主張することになる。
  const verdict =
    diff >= TOLERANCE_SEC
      ? `持ち時間内（${mmss(diff)} 余る）`
      : diff >= -TOLERANCE_SEC
        ? `ほぼ持ち時間ちょうど（誤差${TOLERANCE_SEC}秒以内）`
        : `⚠️ ${mmss(-diff)} 超過`;
  console.log(`${label.padEnd(6, "　")} デモ以外 ${mmss(core / rate)} + デモ 3:00 = ${mmss(total)}   ${verdict}`);
}
