# 分拣桌宠（AIRI）发来的请求：AIRI 说一句话会发好几个请求，只有真正的聊天请求才交给 AstrBot
#   聊天请求：最后一条 user 消息像 "[2026-10-25 13:49] 我说今天挺想你的。\n[Context]\n- system:..."
#   辅助请求：user 内容是 JSON（判断说完没有、修正识别结果），插件自己回答，不花钱
#   看图请求：消息里带 image_url
import json
import re

TIMESTAMP = re.compile(r"^\s*\[\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}(:\d{2})?\]\s*")
CONTEXT = re.compile(r"(^|\n)\s*\[Context\]")


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def last_user_text(body: dict) -> str:
    users = [m for m in (body or {}).get("messages") or [] if isinstance(m, dict) and m.get("role") == "user"]
    return text_of(users[-1].get("content")).strip() if users else ""


def _looks_like_json(text: str) -> bool:
    if not text[:1] in ("[", "{"):
        return False
    try:
        json.loads(text)
        return True
    except Exception:
        return False


def is_chat_request(body: dict) -> bool:
    text = last_user_text(body)
    if not text or _looks_like_json(text):
        return False
    return bool(TIMESTAMP.search(text) or CONTEXT.search(text))


def extract_speech(text: str) -> str:
    """只留下真正说的话：删掉 [Context] 及之后的内容，再去掉开头的 [时间戳]"""
    return TIMESTAMP.sub("", CONTEXT.split(str(text or ""))[0], count=1).strip()


def is_vision_request(body: dict) -> bool:
    for m in (body or {}).get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
            return True
    return False


def vision_parts(body: dict) -> tuple[str, str, list[str]]:
    """看图请求 → (system 提示词, 用户文字, 图片 data URL 列表)"""
    system = "\n".join(text_of(m.get("content")) for m in body.get("messages") or [] if m.get("role") == "system").strip()
    texts, images = [], []
    for m in body.get("messages") or []:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            if content.strip():
                texts.append(content)
            continue
        for p in content or []:
            if p.get("type") == "text" and (p.get("text") or "").strip():
                texts.append(p["text"])
            elif p.get("type") == "image_url":
                url = p.get("image_url")
                url = url.get("url") if isinstance(url, dict) else url
                if isinstance(url, str) and url:
                    images.append(url)
    return system, "\n".join(texts), images


def local_helper_reply(body: dict, user_text: str) -> str:
    """麦克风的辅助请求：判断说完没有 → 只靠停顿判断；修正识别结果 → 原样返回"""
    system = "\n".join(text_of(m.get("content")) for m in body.get("messages") or [] if m.get("role") == "system")
    if re.search(r"finished a spoken message", system, re.I):
        return '{"complete": true}'
    if re.search(r"clean up text that speech recognition produced", system, re.I):
        try:
            segments = json.loads(user_text)
            if isinstance(segments, list):
                return json.dumps([str(s) for s in segments], ensure_ascii=False)
        except Exception:
            pass
    return user_text
