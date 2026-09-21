import { test, expect, Page } from "@playwright/test";

/**
 * シーンごとに test を分ける。
 *
 * Playwright の録画は、同じ Page の中で page.goto() を跨ぐと
 * 遷移後の画面が映像に残らない。画面を渡り歩くデモではここを踏むので、
 * 1 シーン 1 test にして、あとで ffmpeg concat でつなぐ。
 *
 * 各 test の実時間がそのままそのシーンの尺になる。ナレーション側の
 * 実測（narration/demo.ja.json）に合わせてある。
 */

const SCENE = {
  request: 12.8,
  all: 23.6,
  impossible: 19.8,
  aiwork: 41.8,
  review: 25.2,
  mine: 10.2,
  swap: 23.6,
};

/** その時間ぶん画面を見せる。1シーンの残り時間を使い切るのに使う。 */
async function dwell(page: Page, started: number, seconds: number) {
  const left = seconds * 1000 - (Date.now() - started);
  if (left > 0) await page.waitForTimeout(left);
}

/** ゆっくり下までスクロールする。一気に飛ぶと何が起きたか分からない。 */
async function slowScroll(page: Page, to: number, ms: number) {
  const steps = Math.max(1, Math.round(ms / 40));
  for (let i = 1; i <= steps; i++) {
    await page.evaluate((y) => window.scrollTo(0, y), (to * i) / steps);
    await page.waitForTimeout(40);
  }
}

test("01 希望を出す", async ({ page }) => {
  const t0 = Date.now();
  await page.goto("/request");
  await page.waitForTimeout(1800);

  // 入りたい時間を引く。3コマ固定の選択ではなく、1時間刻みで引ける
  const days = await page.$$eval("[data-day]", (els) =>
    Array.from(new Set(els.map((e) => e.getAttribute("data-day")))).filter(Boolean)
  );
  // 1セルずつなぞる。始点と終点だけを hover すると、画面側が待っている
  // 途中のセルの mouseover が発生せず、2コマしか塗られない
  for (const [i, day] of [days[1], days[3]].entries()) {
    const [from, to] = i === 0 ? [10, 16] : [16, 22];
    await page.locator(`.hr[data-day="${day}"][data-hour="${from}"]`).hover();
    await page.mouse.down();
    for (let h = from + 1; h <= to; h++) {
      await page.locator(`.hr[data-day="${day}"][data-hour="${h}"]`).hover();
      await page.waitForTimeout(70);
    }
    await page.mouse.up();
    await page.waitForTimeout(900);
  }

  // 引いた時間から予想の給与が出る
  await expect(page.locator("#amt")).not.toHaveText("0円");
  await page.locator("#amt").scrollIntoViewIfNeeded();
  await dwell(page, t0, SCENE.request);
});

test("02 全員のシフト", async ({ page }) => {
  const t0 = Date.now();
  await page.goto("/all");
  await page.waitForTimeout(1800);
  await slowScroll(page, 2600, 18000);
  await dwell(page, t0, SCENE.all);
});

test("03 組めない週", async ({ page }) => {
  const t0 = Date.now();
  await page.goto("/all?week=impossible");
  await expect(page.locator("h1")).toContainText("組めません");
  // ナレーションが「シフトを出しません」と言い終わるまで見出しを残す。
  // 先にスクロールすると、肝心の一行が読まれない
  await page.waitForTimeout(11000);
  await slowScroll(page, 230, 2500);
  await dwell(page, t0, SCENE.impossible);
});

test("04 AIがゆずる条件を決める", async ({ page }) => {
  const t0 = Date.now();
  await page.goto("/all?week=proposal");
  await expect(page.locator(".tried")).toBeVisible();
  // AI の見立てを1つずつ読ませる。ここが今回いちばん見せたいところ
  await page.waitForTimeout(6000);
  await slowScroll(page, 150, 2500);
  await page.waitForTimeout(7000);
  await slowScroll(page, 330, 2500);
  await page.waitForTimeout(7000);
  await slowScroll(page, 520, 2500);
  await page.waitForTimeout(6000);
  await slowScroll(page, 700, 2500);
  await dwell(page, t0, SCENE.aiwork);
});

test("05 確認待ち", async ({ page }) => {
  const t0 = Date.now();
  await page.goto("/review");
  await page.waitForTimeout(2500);
  // 指示文が混ざっていたものを順に見せる
  await slowScroll(page, 420, 4500);
  await page.waitForTimeout(3500);
  await slowScroll(page, 900, 4500);
  await page.waitForTimeout(3500);
  await slowScroll(page, 1350, 4000);
  await dwell(page, t0, SCENE.review);
});

test("06 自分のシフト", async ({ page }) => {
  const t0 = Date.now();
  await page.goto("/");
  await page.waitForTimeout(3500);
  await page.locator("a", { hasText: "交代を頼む" }).first().hover();
  await dwell(page, t0, SCENE.mine);
});

test("07 代われる人を探す", async ({ page }) => {
  const t0 = Date.now();
  await page.goto("/swap?me=S01&date=2026-10-08&slot=mid");
  await expect(page.locator(".who")).toContainText("代われます");
  await dwell(page, t0, SCENE.swap);
});
