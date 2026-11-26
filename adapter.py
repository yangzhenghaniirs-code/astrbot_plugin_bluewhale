# blue whale 平台：桌宠就是 AstrBot 的一个「聊天平台」，和 QQ、网页聊天一样
#   桌宠（AIRI）→ 18100 端口（OpenAI 兼容接口，和 DSH 版一模一样，桌宠不用改）→ 这里把话变成 AstrBot 的消息事件
#   → AstrBot 完整处理（人设、对话记录、插件、模型）→ 回复被 send() 收下 → 清成口语、补动作 → 交回桌宠念
# 桌宠托盘「切换聊天」用的接口：
#   GET  /dafei/sessions → AstrBot 里桌宠的对话列表；POST /dafei/session {id} 切换，{new:true} 新建
#   GET  /dafei/pending  → 后续消息（提醒、主动搭话）有几条；通知请求（内容含 [dafei-pending]）把它们取走
import asyncio
import json
import time
import uuid

from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from .route import (
    extract_speech,
    is_chat_request,
    is_vision_request,
    last_user_text,
    local_helper_reply,
    vision_parts,
)
from .speech import speakable, split_sentences
from .tts import TTSError, synthesize

TAG = "[blue whale]"
ADAPTER = "bluewhale"
MODEL = "dsh"  # 桌宠里意识模块选的模型名，沿用 DSH 版，桌宠不用改
PENDING_MARK = "[dafei-pending]"

# 插件设置（main.py 启动时填进来）：context、idle_motions、outfit_motions、timeout、fallback_reply
SHARED: dict = {}


def chain_text(chain: MessageChain | None) -> str:
    if not chain or not getattr(chain, "chain", None):
        return ""
    return "".join(c.text for c in chain.chain if isinstance(c, Plain) and c.text).strip()


class BlueWhaleEvent(AstrMessageEvent):
    """一句话的处理过程：收下所有回复，处理完了通知等着的人"""

    def __init__(self, message_str, message_obj, platform_meta, session_id, adapter: "BlueWhaleAdapter") -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._bw_adapter = adapter
        self._bw_texts: list[str] = []
        self._bw_done = asyncio.Event()
        self._bw_first = asyncio.Event()  # 收到第一段了
        self._bw_closed = False

    def _bw_take(self, text: str) -> None:
        if not text:
            return
        if self._bw_closed:  # 这一句已经交出去了，后来才到的（分段的后几段、后台任务的结果等）当成后续消息
            self._bw_adapter.push_pending(text)
            return
        self._bw_texts.append(text)
        if SHARED.get("split_bubbles", True):
            # 一段一个气泡：第一段马上交给桌宠，后面的一段一段进后续消息
            self._bw_closed = True
            self._bw_first.set()

    async def send(self, message: MessageChain) -> None:
        self._bw_take(chain_text(message))
        await super().send(message)

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        # 用户在 AstrBot 里开了流式输出时走这里：整段收齐再交给桌宠
        parts = []
        async for chain in generator:
            t = chain_text(chain)
            if t:
                parts.append(t)
        self._bw_take("".join(parts))
        await super().send_streaming(generator, use_fallback)

    def cleanup_temporary_local_files(self) -> None:
        # AstrBot 处理完一个事件（不管成功、出错、被拦下）最后都会调用这个
        try:
            super().cleanup_temporary_local_files()
        finally:
            self._bw_closed = True
            self._bw_done.set()


@register_platform_adapter(
    ADAPTER,
    "blue whale 桌宠（大肥鱼 Live2D）：桌宠在 18100 端口连进来，和她当面说话",
    default_config_tmpl={"port": 18100, "session_name": "owner", "sender_name": "主人"},
    adapter_display_name="blue whale 桌宠",
    support_streaming_message=False,
)
class BlueWhaleAdapter(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.port = int(platform_config.get("port") or 18100)
        self.session_name = str(platform_config.get("session_name") or "owner")
        self.sender_name = str(platform_config.get("sender_name") or "主人")
        self.metadata = PlatformMetadata(
            name=ADAPTER,
            description="blue whale 桌宠",
            id=platform_config.get("id") or ADAPTER,
            support_streaming_message=False,
            support_proactive_message=True,
        )
        self._lock = asyncio.Lock()  # 同一时间只让角色处理一句话，后来的排队
        self._pending: list[tuple[float, str]] = []
        self._stop = asyncio.Event()
        self._runner: web.AppRunner | None = None

    # ---------- AstrBot 平台必备 ----------
    def meta(self) -> PlatformMetadata:
        return self.metadata

    @property
    def umo(self) -> str:
        return f"{self.metadata.id}:{MessageType.FRIEND_MESSAGE.value}:{self.session_name}"

    async def send_by_session(self, session: MessageSesion, message_chain: MessageChain) -> None:
        # 别的插件主动发给桌宠的话（定时提醒等）：存进后续消息，桌宠来取时念出来
        self.push_pending(chain_text(message_chain))
        await super().send_by_session(session, message_chain)

    async def run(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)  # 截图可能很大
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        while not self._stop.is_set():
            try:
                await web.TCPSite(self._runner, "127.0.0.1", self.port).start()
                logger.info(f"{TAG} 桌宠接口已启动：http://127.0.0.1:{self.port}/v1（会话 {self.umo}）")
                break
            except OSError:
                logger.warning(f"{TAG} 端口 {self.port} 被占用了（DSH 版 blue whale 是不是还开着？），10 秒后再试")
                try:
                    await asyncio.wait_for(self._stop.wait(), 10)
                except asyncio.TimeoutError:
                    pass
        await self._stop.wait()

    async def terminate(self) -> None:
        self._stop.set()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ---------- 后续消息 ----------
    def push_pending(self, raw: str) -> None:
        text = speakable(raw, SHARED.get("idle_motions", []), SHARED.get("outfit_motions", True))
        if text:
            self._pending.append((time.time(), text))
            self._pending = self._pending[-10:]
            logger.info(f"{TAG} 后续消息，等桌宠来取：{text}")

    def _take_pending(self) -> str:
        now = time.time()
        self._pending = [(at, t) for at, t in self._pending if now - at < 30 * 60]  # 半小时前的不念了
        if not self._pending:
            return ""
        if SHARED.get("split_bubbles", True):
            # 每次只交一段：桌宠每 2 秒来取一次，一段一个气泡
            return self._pending.pop(0)[1]
        texts = [t for _, t in self._pending]
        self._pending = []
        return "".join(texts)

    def _forget(self, reply: str) -> None:
        # 这一句的回复已经正常交出去：待说里一样的话删掉，免得念两遍
        import re

        bare = lambda s: re.sub(r"<\|[\s\S]*?\|>", "", s or "").strip()
        self._pending = [(at, t) for at, t in self._pending if bare(t) != bare(reply)]

    # ---------- 把一句话交给 AstrBot ----------
    async def ask(self, text: str) -> str:
        # 同一时间只处理一句：锁要等 AstrBot 把这一句彻底处理完（分段全部发完）才放开，
        # 但第一段可以先交给桌宠，所以这里手动拿锁、交出第一段后在后台等处理完再放
        await self._lock.acquire()
        release_now = True
        try:
            # 新的一句话开始：十分钟前存的后续消息不要了
            self._pending = [(at, t) for at, t in self._pending if time.time() - at < 600]
            abm = AstrBotMessage()
            abm.type = MessageType.FRIEND_MESSAGE
            abm.self_id = ADAPTER
            abm.session_id = self.session_name
            abm.message_id = uuid.uuid4().hex
            abm.sender = MessageMember(user_id=self.session_name, nickname=self.sender_name)
            abm.message = [Plain(text)]
            abm.message_str = text
            abm.raw_message = {"text": text}
            abm.timestamp = int(time.time())
            event = BlueWhaleEvent(text, abm, self.meta(), abm.session_id, self)
            self.commit_event(event)
            timeout = float(SHARED.get("timeout", 180))
            waiters = [asyncio.ensure_future(event._bw_done.wait()), asyncio.ensure_future(event._bw_first.wait())]
            _, pending = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for w in pending:
                w.cancel()
            if not (event._bw_done.is_set() or event._bw_first.is_set()):
                logger.warning(f"{TAG} 等角色回复超过 {timeout:.0f} 秒")
            event._bw_closed = True  # 之后到的回复当成后续消息
            if not event._bw_done.is_set():
                release_now = False
                asyncio.create_task(self._release_when_done(event, timeout))
            return "\n".join(event._bw_texts)
        finally:
            if release_now:
                self._lock.release()

    async def _release_when_done(self, event: BlueWhaleEvent, timeout: float) -> None:
        try:
            await asyncio.wait_for(event._bw_done.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._lock.release()

    async def chat_reply(self, text: str) -> str:
        fallback = SHARED.get("fallback_reply") or "我刚刚走神了，你再说一遍？"
        if not text:
            return fallback
        started = time.time()
        logger.info(f"{TAG} ← 收到：{text}")
        try:
            reply = speakable(await self.ask(text), SHARED.get("idle_motions", []), SHARED.get("outfit_motions", True))
        except Exception as e:
            logger.error(f"{TAG} ✗ 出错：{e}", exc_info=True)
            return fallback
        self._forget(reply)
        logger.info(f"{TAG} → 回复（{time.time() - started:.1f}s）：{reply}")
        return reply or fallback

    async def vision_reply(self, body: dict) -> str:
        # 桌宠截屏 / 收到图片：用 AstrBot 当前的模型看图写描述，不进对话记录
        ctx = SHARED.get("context")
        try:
            provider = ctx.get_using_provider(self.umo) if ctx else None
            if not provider:
                raise RuntimeError("AstrBot 里没有可用的模型")
            system, text, images = vision_parts(body)
            resp = await provider.text_chat(prompt=text or "描述这张图片", image_urls=images, system_prompt=system or None)
            out = (resp.completion_text or "").strip()
            logger.info(f"{TAG} 👁 看图：{out[:80]}")
            return out
        except Exception as e:
            logger.error(f"{TAG} ✗ 看图出错（当前模型可能不支持图片）：{e}")
            return ""

    # ---------- HTTP ----------
    async def _dispatch(self, req: web.Request) -> web.StreamResponse:
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
        if req.method == "OPTIONS":
            return web.Response(status=204, headers=cors)
        import re

        path = re.sub(r"/+", "/", req.path).rstrip("/")
        api = re.sub(r"^/v1", "", path)
        try:
            if req.method == "GET" and api == "/models":
                resp = web.json_response({"object": "list", "data": [{"id": MODEL, "object": "model", "created": 0, "owned_by": "astrbot-bluewhale"}]})
            elif req.method == "POST" and api == "/chat/completions":
                resp = await self._chat_completions(req)
            elif req.method == "POST" and api == "/audio/speech":
                resp = await self._speech(req)
            elif req.method == "GET" and path == "/dafei/sessions":
                resp = web.json_response(await self._list_sessions())
            elif req.method == "POST" and path == "/dafei/session":
                resp = await self._switch_session(req)
            elif req.method == "GET" and path == "/dafei/pending":
                resp = web.json_response({"count": len(self._pending)})
            elif req.method == "GET" and path == "/dafei/health":
                resp = web.json_response({"ok": True})
            else:
                resp = web.json_response({"error": {"message": f"Not found: {req.method} {req.path}", "type": "invalid_request_error"}}, status=404)
        except Exception as e:
            logger.error(f"{TAG} ✗ 处理请求出错：{e}", exc_info=True)
            resp = web.json_response({"error": {"message": "internal error", "type": "server_error"}}, status=500)
        if not resp.prepared:
            resp.headers.update(cors)
        return resp

    async def _chat_completions(self, req: web.Request) -> web.StreamResponse:
        try:
            raw = await req.text()
            body = json.loads(raw) if raw else {}
        except Exception:
            return web.json_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}, status=400)

        user_text = last_user_text(body)
        if PENDING_MARK in user_text:
            logger.info(f"{TAG} ⤷ 桌宠取走后续消息（{len(self._pending)} 条）")
            reply = self._take_pending()
        elif is_vision_request(body):
            reply = await self.vision_reply(body)
        elif is_chat_request(body):
            reply = await self.chat_reply(extract_speech(user_text))
        else:
            reply = local_helper_reply(body, user_text)

        cid = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        if not body.get("stream"):
            return web.json_response({
                "id": cid, "object": "chat.completion", "created": created, "model": MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        # 流式：一句一句发出去
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(req)

        def chunk(delta: dict, finish=None) -> bytes:
            data = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

        try:
            await resp.write(chunk({"role": "assistant", "content": ""}))
            for sentence in split_sentences(reply):
                await resp.write(chunk({"content": sentence}))
            await resp.write(chunk({}, "stop"))
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        except ConnectionResetError:
            pass
        return resp

    async def _speech(self, req: web.Request) -> web.Response:
        # 桌宠的「OpenAI 兼容 TTS」会把要念的字发到这里：转给百炼 CosyVoice（克隆音色），返回 mp3
        err = lambda msg, status: web.json_response({"error": {"message": msg, "type": "tts_error"}}, status=status)
        if not SHARED.get("tts_enabled"):
            return err("插件里没打开语音合成（插件配置 → 语音合成）", 503)
        try:
            body = json.loads(await req.text() or "{}")
        except Exception:
            return err("Invalid JSON body", 400)
        text = str(body.get("input") or "")
        # 桌宠请求里的 voice 如果本身就是 cosyvoice 音色 ID 就用它，否则用插件里设置的
        asked = str(body.get("voice") or "")
        voice = asked if asked.startswith("cosyvoice-") or asked.startswith("qwen-") else SHARED.get("tts_voice", "")
        try:
            audio = await synthesize(
                text,
                api_key=SHARED.get("tts_api_key", ""),
                model=SHARED.get("tts_model", "cosyvoice-v3.5-flash"),
                voice=voice,
                host=SHARED.get("tts_host", ""),
            )
        except TTSError as e:
            logger.error(f"{TAG} ✗ 语音合成出错：{e}")
            return err(str(e), 502)
        except Exception as e:
            logger.error(f"{TAG} ✗ 语音合成出错：{type(e).__name__}", exc_info=True)
            return err("internal error", 500)
        logger.info(f"{TAG} 🔊 合成 {len(text)} 字 → {len(audio) // 1024}KB")
        return web.Response(body=audio, content_type="audio/mpeg")

    async def _list_sessions(self) -> dict:
        ctx = SHARED.get("context")
        if not ctx:
            return {"current": None, "sessions": []}
        cm = ctx.conversation_manager
        current = await cm.get_curr_conversation_id(self.umo)
        convs = await cm.get_conversations(self.umo)
        sessions = []
        for c in convs:
            when = int(c.updated_at or c.created_at or 0)
            title = (c.title or "").strip() or time.strftime("对话 %m-%d %H:%M", time.localtime(c.created_at or when or time.time()))
            sessions.append({"id": c.cid, "title": title[:30], "active": when * 1000})
        sessions.sort(key=lambda s: s["active"], reverse=True)
        return {"current": current, "sessions": sessions[:30]}

    async def _switch_session(self, req: web.Request) -> web.Response:
        ctx = SHARED.get("context")
        try:
            body = json.loads(await req.text() or "{}")
        except Exception:
            body = {}
        if not ctx:
            return web.json_response({"ok": False, "error": "AstrBot 还没准备好"}, status=503)
        cm = ctx.conversation_manager
        if body.get("new"):
            cid = await cm.new_conversation(self.umo, platform_id=self.metadata.id)
            logger.info(f"{TAG} 新建对话 {cid}")
            return web.json_response({"ok": True, "current": cid})
        ids = {c.cid for c in await cm.get_conversations(self.umo)}
        if body.get("id") not in ids:
            return web.json_response({"ok": False, "error": f"找不到对话 {body.get('id')}"}, status=404)
        await cm.switch_conversation(self.umo, body["id"])
        logger.info(f"{TAG} 切换对话：{body['id']}")
        return web.json_response({"ok": True, "current": body["id"]})
