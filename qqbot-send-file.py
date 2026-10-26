#!/usr/bin/env python3
"""把本地文件发到 QQ（复用 Hermes 的 QQ 机器人适配器）。

为什么不能用 `hermes send`：CLI 的 send_message_tool 里 qqbot 只挂了**纯文本**
路由（`_CHUNKED_ROUTES["qqbot"]`），MEDIA: 会被直接丢掉并回一条
「MEDIA attachments were omitted for qqbot」的 warning。但 QQ 适配器本身是有
`send_document` / `_upload_media` / `ChunkedUploader` 的，接口也支持（MSG_TYPE_MEDIA=7、
MEDIA_TYPE_FILE=4、/v2/users/{openid}/files）。所以这里绕过 CLI，直接用适配器的实现：
分片上传（prepare → PUT parts → complete）拿 file_info，再发一条 msg_type=7 的消息。

依赖：hermes 自带的 venv（httpx）。用 `~/.hermes/hermes-agent/venv/bin/python` 跑。

用法：
    qqbot-send-file.py <本地文件> [caption]
    qqbot-send-file.py --text "只发一段文字"

凭据：~/.hermes/.env 里的 QQ_APP_ID / QQ_CLIENT_SECRET / QQBOT_HOME_CHANNEL。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

HERMES = Path("/home/ryp-1/.hermes/hermes-agent")
sys.path.insert(0, str(HERMES))

import httpx  # noqa: E402  (venv 里有)

from gateway.platforms.qqbot.chunked_upload import ChunkedUploader  # noqa: E402
from gateway.platforms.qqbot.constants import (  # noqa: E402
    API_BASE, TOKEN_URL, MEDIA_TYPE_FILE, MSG_TYPE_MEDIA, MSG_TYPE_TEXT,
    DEFAULT_API_TIMEOUT, FILE_UPLOAD_TIMEOUT,
)

ENV = Path("/home/ryp-1/.hermes/.env")


def load_env() -> dict:
    """读 ~/.hermes/.env 的 KEY=VALUE（只要 QQ_* 这几个）。"""
    out = {}
    if not ENV.exists():
        return out
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k.startswith("QQ"):
            out[k] = v
    return out


class QQBot:
    def __init__(self, app_id: str, secret: str, base: str = API_BASE):
        self.app_id, self.secret, self.base = app_id, secret, base
        self.client = httpx.AsyncClient(timeout=DEFAULT_API_TIMEOUT)
        self.token: str | None = None

    async def ensure_token(self) -> str:
        if self.token:
            return self.token
        r = await self.client.post(TOKEN_URL, json={"appId": self.app_id, "clientSecret": self.secret})
        data = r.json()
        self.token = data.get("access_token")
        if not self.token:
            raise RuntimeError(f"QQ token 获取失败：{data}")
        return self.token

    async def api(self, method: str, path: str, body: dict | None = None, timeout: float = DEFAULT_API_TIMEOUT) -> dict:
        token = await self.ensure_token()
        r = await self.client.request(
            method, f"{self.base}{path}", json=body, timeout=timeout,
            headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
        )
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(f"QQ API [{r.status_code}] {path}: {data.get('message', data)}")
        return data

    async def send_text(self, openid: str, text: str) -> dict:
        return await self.api("POST", f"/v2/users/{openid}/messages",
                              {"content": text, "msg_type": MSG_TYPE_TEXT})

    async def send_file(self, openid: str, path: Path, caption: str = "") -> dict:
        """分片上传 + 发 msg_type=7（富媒体）。返回发送结果。"""
        uploader = ChunkedUploader(api_request=self.api, http_put=self.client.put, log_tag="QQBotFile")
        up = await uploader.upload(
            chat_type="c2c", target_id=openid, file_path=str(path),
            file_type=MEDIA_TYPE_FILE, file_name=path.name)
        file_info = up.get("file_info") or (up.get("data") or {}).get("file_info")
        if not file_info:
            raise RuntimeError(f"上传成功但没拿到 file_info：{up}")
        body = {"msg_type": MSG_TYPE_MEDIA, "media": {"file_info": file_info}}
        if caption:
            body["content"] = caption[:1000]
        return await self.api("POST", f"/v2/users/{openid}/messages", body, timeout=FILE_UPLOAD_TIMEOUT)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="要发送的本地文件")
    ap.add_argument("caption", nargs="?", default="", help="附带的文字说明")
    ap.add_argument("--text", help="只发一段文字（不发文件）")
    ap.add_argument("--to", help="openid（默认取 QQBOT_HOME_CHANNEL）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    env = load_env()
    app_id, secret = env.get("QQ_APP_ID"), env.get("QQ_CLIENT_SECRET")
    openid = args.to or env.get("QQBOT_HOME_CHANNEL") or env.get("QQ_ALLOWED_USERS", "").split(",")[0]
    if not (app_id and secret and openid):
        print("缺少 QQ_APP_ID / QQ_CLIENT_SECRET / QQBOT_HOME_CHANNEL", file=sys.stderr)
        return 2

    bot = QQBot(app_id, secret)
    try:
        if args.text:
            res = await bot.send_text(openid, args.text)
            kind = "text"
        else:
            if not args.file:
                print("要么给文件，要么用 --text", file=sys.stderr)
                return 2
            p = Path(args.file).expanduser()
            if not p.is_file():
                print(f"文件不存在：{p}", file=sys.stderr)
                return 2
            res = await bot.send_file(openid, p, args.caption)
            kind = "file"
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False) if args.json
              else f"发送失败：{exc}", file=sys.stderr)
        return 1
    finally:
        await bot.client.aclose()

    if args.json:
        print(json.dumps({"success": True, "kind": kind, "to": openid, "id": res.get("id")}, ensure_ascii=False))
    else:
        print(f"已发送（{kind}）→ {openid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
