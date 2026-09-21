#!/usr/bin/env node
// スライドのレイアウト崩れを検査する。
//  ① スライド枠からのはみ出し（overflow）
//  ② フッター（ページ番号）と本文の重なり ← overflow では検出できない
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
// Playwright は e2e/ にだけ入っているため、依存はそこから解決する（build_pdf.mjs と同じ）
const { chromium } = createRequire(path.join(repo, "scripts/demo/package.json"))("@playwright/test");
const DECK = path.join(repo, "docs/pitch/deck.html");

const b = await chromium.launch();
try {
  const p = await b.newPage({ viewport: { width: 1280, height: 720 } });
  await p.goto(pathToFileURL(DECK).href, { waitUntil: "networkidle" });

  const issues = await p.evaluate(() => {
    const out = [];
    document.querySelectorAll("section.slide").forEach((slide, i) => {
      const n = i + 1;
      const sr = slide.getBoundingClientRect();

      // ① はみ出し
      if (slide.scrollHeight > slide.clientHeight + 2)
        out.push(`スライド${n}: 縦にはみ出し (${slide.scrollHeight} > ${slide.clientHeight})`);
      if (slide.scrollWidth > slide.clientWidth + 2)
        out.push(`スライド${n}: 横にはみ出し (${slide.scrollWidth} > ${slide.clientWidth})`);

      // ② フッターと本文の重なり
      const footer = slide.querySelector(".slide-footer, .footer, .page-num");
      if (!footer) return;
      const fr = footer.getBoundingClientRect();
      if (fr.width === 0 || fr.height === 0) return;
      for (const el of slide.querySelectorAll("*")) {
        if (el === footer || footer.contains(el) || el.contains(footer)) continue;
        if (!el.getClientRects().length) continue;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const ov = Math.min(r.right, fr.right) - Math.max(r.left, fr.left);
        const oh = Math.min(r.bottom, fr.bottom) - Math.max(r.top, fr.top);
        if (ov > 2 && oh > 2) {
          out.push(`スライド${n}: フッターに <${el.tagName.toLowerCase()}${el.className ? "." + String(el.className).split(" ")[0] : ""}> が重なっている`);
          break;
        }
      }
      // 枠外にフッターが出ていないか
      if (fr.bottom > sr.bottom + 2) out.push(`スライド${n}: フッターがスライド枠の外`);
    });
    return out;
  });

  if (issues.length) {
    console.log("⚠️ レイアウトの問題:");
    for (const i of issues) console.log("   " + i);
    process.exitCode = 1;
  } else {
    console.log("✅ レイアウト: はみ出し・フッター重なりなし");
  }
} finally {
  await b.close();
}
