/**
 * サービス説明資料（deck.html）を 16:9 の PDF に書き出す。
 *
 *   node scripts/pitch/build_pdf.mjs
 *
 * 本選の発表は運営が用意した共用PCにHDMI接続する形式のため、ローカルHTMLは開けない。
 * PDF にしておけば、Google Drive 等から共用PCのブラウザで開いてそのまま投影できる。
 *
 * Google スライドで開きたい場合は PDF を読み込めないので build_pptx.mjs を使う。
 * deck.html を直したら両方を作り直すこと。
 *
 * Playwright は e2e/ にだけ入っているため、依存はそこから解決する
 * （このスクリプト自体は資料ビルドなので e2e/ ではなく scripts/ に置く）。
 */
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import fs from "node:fs";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const { chromium } = createRequire(path.join(repo, "scripts/demo/package.json"))("@playwright/test");
const deck = path.join(repo, "docs/pitch/deck.html");
const out = path.join(repo, "docs/pitch/kumu_シフト作成AI.pdf");

if (!fs.existsSync(deck)) {
  console.error(`deck が見つかりません: ${deck}`);
  process.exit(1);
}

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  // pathToFileURL で file URL を生成する（空白や非ASCIIを含むパスでも正しくエンコードされる）。
  // assets/ は相対参照なので deck.html と同階層のまま解決される。
  await page.goto(pathToFileURL(deck).href, { waitUntil: "networkidle" });

  await page.pdf({
    path: out,
    // deck.html の @page と揃える（16:9）
    width: "13.333in",
    height: "7.5in",
    printBackground: true,
    margin: { top: 0, right: 0, bottom: 0, left: 0 },
  });
} finally {
  // 途中で失敗しても Chromium プロセスを残さない
  await browser.close();
}

const slides = (fs.readFileSync(deck, "utf8").match(/<section class="slide"/g) || []).length;
const kb = Math.round(fs.statSync(out).size / 1024);
console.log(`OK: ${out}`);
console.log(`  スライド数: ${slides} / サイズ: ${kb} KB`);
