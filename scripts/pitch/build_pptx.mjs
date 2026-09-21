/**
 * サービス説明資料（deck.html）を 16:9 の PowerPoint（.pptx）に書き出す。
 *
 *   node scripts/pitch/build_pptx.mjs
 *
 * Google スライドは .pptx をそのまま読み込めるが、PDF は読み込めない。
 * 共用PCのブラウザから Google スライドで投影したい場合はこちらを使う。
 *
 * 各スライドを 1 枚の画像として貼るため、HTML と見た目が完全に一致する
 * （フォント・レイアウト崩れが起きない）。引き換えに文字は編集できない。
 * 文言を直すときは deck.html を直してこのスクリプトを再実行する。
 *
 * 画像の書き出しは Playwright、pptx の組み立ては python-pptx が担当する。
 */
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import os from "node:os";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const { chromium } = createRequire(path.join(repo, "scripts/demo/package.json"))("@playwright/test");
const deck = path.join(repo, "docs/pitch/deck.html");
const out = path.join(repo, "docs/pitch/kumu_シフト作成AI.pptx");

// 1280x720 の 2倍。Google スライドは 1 スライド 1920x1080 相当で表示されるため、
// これ以上大きくしてもファイルが重くなるだけで投影時の見た目は変わらない。
const W = 1280;
const H = 720;
const SCALE = 2;

if (!fs.existsSync(deck)) {
  console.error(`deck が見つかりません: ${deck}`);
  process.exit(1);
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "deck-pptx-"));
const shots = [];

const browser = await chromium.launch();
try {
  const page = await browser.newPage({
    viewport: { width: W, height: H },
    deviceScaleFactor: SCALE,
  });
  await page.goto(pathToFileURL(deck).href, { waitUntil: "networkidle" });

  const slides = page.locator("section.slide");
  const n = await slides.count();
  // ここで process.exit すると finally が走らず Chromium が残るため throw する
  if (n === 0) {
    throw new Error("スライドが 1 枚も見つかりません（deck.html の構造が変わった可能性）");
  }

  for (let i = 0; i < n; i++) {
    const p = path.join(tmp, `slide-${String(i + 1).padStart(2, "0")}.png`);
    await slides.nth(i).screenshot({ path: p });
    shots.push(p);
  }
  console.log(`  スライド ${n} 枚を書き出しました`);
} finally {
  // 途中で失敗しても Chromium プロセスを残さない
  await browser.close();
}

// python-pptx で組み立てる。画像パスは引数で渡さず一覧ファイル経由にする
// （非ASCIIパスや枚数の増加に影響されないため）。
const listFile = path.join(tmp, "slides.txt");
fs.writeFileSync(listFile, shots.join("\n"), "utf8");

try {
  execFileSync("python3", [path.join(here, "_pptx_from_images.py"), listFile, out], {
    stdio: "inherit",
  });
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}

const kb = Math.round(fs.statSync(out).size / 1024);
console.log(`OK: ${out}`);
console.log(`  サイズ: ${kb} KB`);
