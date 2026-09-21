#!/usr/bin/env python3
"""録画した素材とナレーションを1本の mp4 にする。

    python3 render.py demo

ナレーションの配置は adelay + amix で行う。-itsoffset は amix と
一緒に使うと効かず、全部の cue が先頭に重なる。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "output" / "raw"
OUT = HERE / "output"
WORK = OUT / "work"


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(" ".join(cmd[:6]), "...", file=sys.stderr)
        print(proc.stderr[-1500:], file=sys.stderr)
        raise SystemExit(f"失敗: {cmd[0]}")


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    return float(out or 0)


def concat_video(name: str) -> Path:
    """シーンごとの webm を順につなぐ。

    録画は 1 テスト 1 ファイルになる。ディレクトリ名の先頭に番号を
    振ってあるので、名前順でシーンの順序になる。
    """
    parts = sorted(RAW.glob(f"{name}-*/video.webm"), key=lambda p: p.parent.name)
    if not parts:
        raise SystemExit(f"素材が見つかりません: {RAW}/{name}-*/video.webm")
    print(f"素材 {len(parts)} 本")
    for p in parts:
        print(f"  {p.parent.name}  {duration(p):.1f}s")

    WORK.mkdir(parents=True, exist_ok=True)
    listfile = WORK / f"{name}.txt"
    listfile.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8"
    )
    joined = WORK / f"{name}.webm"
    # 同じコーデックなので再エンコードせずにつなぐ
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(joined)])
    return joined


def build_audio(name: str, video_sec: float) -> Path:
    spec = json.loads((HERE / "narration" / f"{name}.ja.json").read_text(encoding="utf-8"))
    voice, rate = spec["voice"], spec.get("rate", "+0%")
    cues = spec["cues"]

    WORK.mkdir(parents=True, exist_ok=True)
    mp3s = []
    for i, cue in enumerate(cues):
        path = WORK / f"{name}-cue{i:02d}.mp3"
        if not path.exists():
            run(["uvx", "edge-tts", "--voice", voice, "--rate", rate,
                 "--text", cue["text"], "--write-media", str(path)])
        mp3s.append(path)
        print(f"  cue{i:02d} at={cue['at']:>5.1f}s  {duration(path):.1f}s")

    # 動画と同じ長さの無音を土台にして、その上に cue を重ねる
    cmd = ["ffmpeg", "-y",
           "-f", "lavfi", "-t", f"{video_sec:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
    for p in mp3s:
        cmd += ["-i", str(p)]

    filters = []
    labels = ["[0:a]"]
    for i, cue in enumerate(cues):
        ms = int(round(cue["at"] * 1000))
        filters.append(f"[{i + 1}:a]adelay={ms}|{ms},volume=1.6[c{i}]")
        labels.append(f"[c{i}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[out]"
    )

    aac = WORK / f"{name}.aac"
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]",
            "-c:a", "aac", "-b:a", "160k", str(aac)]
    run(cmd)
    return aac


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "demo"
    video = concat_video(name)
    sec = duration(video)
    print(f"つないだ映像: {sec:.1f}s")

    print("ナレーションを作っています")
    audio = build_audio(name, sec)

    OUT.mkdir(parents=True, exist_ok=True)
    mp4 = OUT / f"{name}.mp4"
    run(["ffmpeg", "-y", "-i", str(video), "-i", str(audio),
         "-c:v", "libx264", "-preset", "slow", "-crf", "23",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         "-c:a", "aac", "-b:a", "160k", "-shortest", str(mp4)])
    size = mp4.stat().st_size / 1_000_000
    print(f"\n出力: {mp4}  {duration(mp4):.1f}s / {size:.1f}MB")


if __name__ == "__main__":
    main()
