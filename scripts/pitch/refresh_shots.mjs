/**
 * スライド用スクリーンショットをデプロイ環境から撮り直す。
 *
 *   node scripts/pitch/refresh_shots.mjs
 *
 * UIの文言やレイアウトを変えたら必ず実行する。古い画像のまま本番に臨むと、
 * 実機デモとスライドで表示が食い違う（実際「1日進める▶」のまま残っていた）。
 */
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const { chromium } = createRequire(path.join(repo, "scripts/demo/package.json"))("@playwright/test");
const BASE = process.env.DECK_SHOT_BASE ?? "https://moraiwasure-web-zjl6dzb7gq-an.a.run.app";
const OUT = path.join(repo, "docs/pitch/assets");

const b = await chromium.launch();
try {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1100 }, locale: "ja-JP", deviceScaleFactor: 2 });
  const p = await ctx.newPage();
  await p.goto(`${BASE}/consult`, { waitUntil: "networkidle" });

  // Cloud Run のコールドスタート中はセッション生成が終わるまでサンプルが disabled のまま。
  // deployed-smoke と同じく最大120秒待つ。
  const sample = p.getByRole("button", { name: /さくらさん/ });
  await sample
    .waitFor({ state: "visible", timeout: 120000 })
    .catch(() => {
      throw new Error(`サンプルボタンが表示されませんでした: ${BASE}/consult が起動しているか確認してください`);
    });
  for (let i = 0; i < 240 && (await sample.isDisabled()); i++) await p.waitForTimeout(500);
  if (await sample.isDisabled()) {
    throw new Error(
      `サンプルボタンが120秒たっても押せる状態になりませんでした。` +
        `Cloud Run のコールドスタートか API 側の不調が疑われます。` +
        `\`./scripts/deploy.sh warm\` で暖機してから再実行してください。`,
    );
  }
  await sample.click();
  await p.locator("#result").getByText(/¥[0-9,]+/).first().waitFor({ timeout: 120000 });
  await p.waitForTimeout(1500);

  // ① 相談コンソール全景（スライド5）
  // 申請パックを開いた状態で撮る。デモの主役がここなので、畳んだ状態だと
  // スライドの箇条書き（提出先ごとにまとめる）と画像が食い違う。
  const packBtn = p.getByRole("button", { name: "提出先ごとにまとめる" });
  if (await packBtn.count()) {
    await packBtn.click();
    await p.getByText(/か所にまとまります/).waitFor({ timeout: 60000 });
    await p.waitForTimeout(800);
  }
  await p.screenshot({ path: path.join(OUT, "screen-console.png") });
  console.log("  screen-console.png");

  // ② 申請メモ（スライド9）
  await p.getByRole("button", { name: /児童手当.*\/年/ }).first().click();
  await p.getByRole("button", { name: "申請書の下書きを作る" }).click();
  await p.getByText("申請メモ（下書き）").waitFor({ timeout: 60000 });
  await p.waitForTimeout(800);
  // #result 全体や rounded-xl の外枠だと縦3500pxの細長い画像になりスライド上で読めない。
  // 見出し「申請メモ（下書き）」を含む rounded-lg のカードだけを撮る（およそ 472x525）。
  const draft = p
    .getByText("申請メモ（下書き）")
    .locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]");
  await draft.scrollIntoViewIfNeeded();
  await draft.screenshot({ path: path.join(OUT, "crop-draft.png") });
  console.log("  crop-draft.png");

  // ③ Watcher の通知（スライド8）
  await p.getByRole("button", { name: /通知をプレビュー/ }).click();
  await p.waitForTimeout(8000);
  const watcher = p.getByText("見張り（Watcher）").locator("xpath=ancestor::div[contains(@class,'rounded-xl')][1]");
  await watcher.screenshot({ path: path.join(OUT, "crop-watcher.png") });
  console.log("  crop-watcher.png");

  await ctx.close();
} finally {
  await b.close();
}
console.log("完了: docs/pitch/assets を更新しました");
