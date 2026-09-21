import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// kumu の画面は素の HTTP サーバーで動く。ビルド工程が無いので、
// webServer はそのまま起動コマンドを渡すだけでよい。
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const PORT = 8790;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180 * 1000,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1280, height: 720 },
    video: { mode: "on", size: { width: 1280, height: 720 } },
    // 機械的な動きに見えないよう、操作の間に少し溜めを作る
    launchOptions: { slowMo: 60 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  outputDir: path.resolve(__dirname, "output/raw"),
  webServer: {
    command: `uv run python scripts/serve.py --port ${PORT}`,
    cwd: REPO_ROOT,
    url: `http://127.0.0.1:${PORT}/`,
    reuseExistingServer: true,
    timeout: 120 * 1000,
  },
});
