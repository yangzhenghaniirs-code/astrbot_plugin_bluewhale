# 语音合成：把桌宠发来的文字交给阿里云百炼 CosyVoice（可以用克隆音色），返回 mp3
#   桌宠里把语音合成选成「OpenAI 兼容 TTS」，地址指向本插件的 /v1（见 adapter.py 里的 /audio/speech），
#   合成在百炼云端做，这里只负责转发，不占电脑算力。
#   用的是百炼 CosyVoice 的 WebSocket 接口：run-task → 等 task-started → continue-task 发文字 → finish-task → 收音频
import asyncio
import json
import re
import uuid

import aiohttp

DEFAULT_WS = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
MARKER = re.compile(r"<\|[\s\S]*?\|>")


class TTSError(Exception):
    pass


def ws_url(host: str) -> str:
    """设置里填业务空间域名（ws-xxxx.cn-beijing.maas.aliyuncs.com）就走专属域名，留空走通用地址"""
    host = (host or "").strip()
    if not host:
        return DEFAULT_WS
    host = re.sub(r"^(https?|wss?)://", "", host).strip("/")
    host = re.sub(r"/.*$", "", host)
    return f"wss://{host}/api-ws/v1/inference"


def clean(text: str) -> str:
    return MARKER.sub("", text or "").strip()


async def synthesize(text: str, *, api_key: str, model: str, voice: str, host: str = "", timeout: float = 60.0) -> bytes:
    """文字 → mp3 字节。出错抛 TTSError（错误信息里不会带 Key）"""
    text = clean(text)
    if not text:
        raise TTSError("没有可念的文字")
    if not api_key:
        raise TTSError("没填百炼 API Key（插件配置 → 语音合成）")
    if not voice:
        raise TTSError("没填音色 ID（插件配置 → 语音合成）")

    task_id = uuid.uuid4().hex
    audio = bytearray()
    headers = {"Authorization": f"bearer {api_key}", "User-Agent": "astrbot-plugin-bluewhale"}

    def cmd(action: str, payload: dict) -> str:
        return json.dumps({"header": {"action": action, "task_id": task_id, "streaming": "duplex"}, "payload": payload}, ensure_ascii=False)

    async def run() -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url(host), headers=headers, max_msg_size=0, heartbeat=20) as ws:
                await ws.send_str(cmd("run-task", {
                    "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer", "model": model,
                    "parameters": {"text_type": "PlainText", "voice": voice, "format": "mp3", "sample_rate": 22050},
                    "input": {},
                }))
                started = False
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        audio.extend(msg.data)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        event = (data.get("header") or {}).get("event")
                        if event == "task-started" and not started:
                            started = True
                            await ws.send_str(cmd("continue-task", {"input": {"text": text}}))
                            await ws.send_str(cmd("finish-task", {"input": {}}))
                        elif event == "task-finished":
                            return
                        elif event == "task-failed":
                            h = data.get("header") or {}
                            raise TTSError(f"百炼返回失败：{h.get('error_code', '')} {h.get('error_message', '')}".strip())
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        break

    try:
        await asyncio.wait_for(run(), timeout)
    except asyncio.TimeoutError:
        raise TTSError(f"百炼合成超时（{timeout:.0f} 秒）")
    except aiohttp.WSServerHandshakeError as e:
        hint = "（Key 不对，或 Key 和接入域名不匹配）" if e.status in (401, 403) else ""
        raise TTSError(f"连不上百炼：HTTP {e.status}{hint}")
    except aiohttp.ClientError as e:
        raise TTSError(f"连不上百炼：{type(e).__name__}")
    if not audio:
        raise TTSError("百炼没有返回声音（音色 ID 和模型对不上？音色创建时用的模型要和这里的模型一样）")
    return bytes(audio)
