"""別のサイトから、店長の確認を勝手に通せないこと。

この作品の security の芯は「モデルが騙されても、店長の確認を通らないと
制約にならない」こと。その確認を外から叩けるなら、芯のほうが抜けている。

サーバーを実際に起動して確かめる。ハンドラだけを呼ぶ形にすると、
ヘッダの扱いを取り違えたまま通ってしまう。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "serve.py"), "--port", str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE, timeout=1).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("画面サーバーが起動しなかった")
    yield BASE
    proc.terminate()
    proc.wait(timeout=10)


def _post(path: str, body: bytes, **headers) -> int:
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k.replace("_", "-"), v)
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


class Test別サイトからの送信を拒む:
    def test_フォームの形では通らない(self, server):
        """enctype="text/plain" のフォームは、本文を JSON の形に組める。
        プリフライトも起きないので、Content-Type だけが頼りになる。"""
        body = b'{"key":"csrf_probe","action":"accept","pad":"="}'
        code = _post(
            "/decide", body, Content_Type="text/plain", Origin="https://evil.example"
        )
        assert code == 403

    def test_別オリジンのJSONも拒む(self, server):
        body = b'{"key":"csrf_probe","action":"accept"}'
        code = _post(
            "/decide",
            body,
            Content_Type="application/json",
            Origin="https://evil.example",
        )
        assert code == 403

    def test_urlencodedのフォームも拒む(self, server):
        code = _post(
            "/decide",
            b"key=csrf_probe&action=accept",
            Content_Type="application/x-www-form-urlencoded",
            Origin="https://evil.example",
        )
        assert code == 403

    def test_ackも同じように守る(self, server):
        body = b'{"key":"csrf_probe","on":true,"pad":"="}'
        code = _post(
            "/ack", body, Content_Type="text/plain", Origin="https://evil.example"
        )
        assert code == 403


class Test画面自身からの送信は通す:
    def test_同一オリジンのJSONは通る(self, server):
        body = json.dumps({"key": "selftest_probe", "action": ""}).encode()
        code = _post(
            "/decide",
            body,
            Content_Type="application/json",
            Origin=f"http://127.0.0.1:{PORT}",
        )
        assert code == 200

    def test_Originが無い送信は通す(self, server):
        """ブラウザは別オリジンへの POST に必ず Origin を付ける。
        付いていないのはブラウザ以外からなので、そこは塞がない。"""
        body = json.dumps({"key": "selftest_probe", "action": ""}).encode()
        code = _post("/decide", body, Content_Type="application/json")
        assert code == 200
