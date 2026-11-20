# 角色的回复 → 桌宠念的文字（从 DSH 版 blue whale 的 clean-text.js / idle-motion.js / outfit.js 搬过来）
#   clean_for_speech：去掉 markdown、网址、emoji，多行合成一段；<|ACT ...|> 这些标记原样保留
#   split_sentences：流式返回时一句一句切开
#   with_outfit_motions：换装表情换成换装动作，衣服才能一直保持
#   with_idle_motion：回复里没写动作时补一个说话动作
import json
import random

try:
    import regex as re  # 支持 \p{...}，AstrBot 自带

    EMOJI = re.compile(r"[\p{Extended_Pictographic}\p{Regional_Indicator}\U0001F3FB-\U0001F3FF️‍⃣]")
except ImportError:  # pragma: no cover
    import re

    EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF️‍⃣]")

END_PUNCT = re.compile(r"[。！？!?；;，,、：:…~]$")
# AIRI 的控制标记：<|ACT {...}|>、<|DELAY 1|> 等
MARKER = re.compile(r"<\|[\s\S]*?\|>")
# 清洗时先把标记换成占位符（私用区字符 + 序号），最后再换回来
PLACEHOLDER = re.compile("(\\d+)")


def clean_for_speech(text) -> str:
    markers: list[str] = []

    def keep(m):
        markers.append(m.group(0))
        return f"{len(markers) - 1}"

    s = str(text or "")
    s = re.sub(r"<think>[\s\S]*?</think>", " ", s, flags=re.I)
    s = MARKER.sub(keep, s)
    s = re.sub(r"```[\s\S]*?(```|$)", " ", s)  # 代码块整段去掉（包括没闭合的）
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)  # 图片 → 替代文字
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)  # 链接 → 链接文字
    s = re.sub(r"https?://\S+", " ", s)  # 裸网址不念
    s = re.sub(r"</?[a-zA-Z][^>]*>", " ", s)  # HTML 标签
    s = re.sub(r"`([^`]*)`", r"\1", s)  # 行内代码保留内容
    s = EMOJI.sub("", s)

    lines = []
    for line in re.split(r"\r?\n", s):
        line = re.sub(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*$", "", line)  # 表格分隔行、分隔线
        line = re.sub(r"^\s*([*_]\s*){3,}$", "", line)
        line = re.sub(r"^\s{0,3}(#{1,6}|>+)\s*", "", line)  # 标题、引用
        line = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", line)  # 列表符号
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)  # 粗体
        line = line.replace("~~", "").replace("*", "")
        line = re.sub(r"\s*\|\s*", "，", line)  # 表格竖线
        line = re.sub(r"^[\s，]+|[\s，]+$", "", line).strip()
        if line:
            lines.append(line)

    # 多行合成一段：没有标点结尾的行补一个句号（只有标记的行不补）
    out = []
    for i, line in enumerate(lines):
        bare = PLACEHOLDER.sub("", line).strip()
        out.append(line + "。" if i < len(lines) - 1 and bare and not END_PUNCT.search(bare) else line)
    s = "".join(out)

    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"[，,]\s*([。！？!?])", r"\1", s)
    s = re.sub(r"([。！？!?])\1+", r"\1", s)
    s = s.strip()
    s = PLACEHOLDER.sub(lambda m: markers[int(m.group(1))], s)
    return re.sub(r"<\|DELAY[\s\S]*?\|>", "", s, flags=re.I)  # 延迟标记不读


def split_sentences(text: str) -> list[str]:
    return re.findall(r"(?:<\|[\s\S]*?\|>|[^。！？!?；;\n])+[。！？!?；;]*", text or "")


# ---- 换装 ----
OUTFITS = {"长裙", "中号", "小号", "大号", "脱鞋", "尾巴开关", "胸口衣服", "穿鞋", "短裙", "放出尾巴", "换回胸口衣服"}
ALIASES = {"裙子切换": "长裙", "换回裙子": "短裙", "体型1": "中号", "体型2": "小号", "默认体型": "大号"}


def with_outfit_motions(reply: str, enabled: bool = True) -> str:
    if not reply or not enabled:
        return reply

    def repl(m):
        try:
            act = json.loads(m.group(1))
        except Exception:
            return m.group(0)
        if not isinstance(act, dict) or "expression" not in act:
            return m.group(0)
        expression = act.pop("expression")
        if expression is None:
            name = "全部还原"
        else:
            raw = expression.get("name") if isinstance(expression, dict) else None
            name = raw if raw in OUTFITS else ALIASES.get(raw)
            if not name:
                act["expression"] = expression
                return m.group(0)
        act["motion"] = f"Animation/换装-{name}.motion3.json"
        return f"<|ACT {json.dumps(act, ensure_ascii=False, separators=(',', ':'))}|>"

    return re.sub(r"<\|ACT\s+([\s\S]*?)\|>", repl, reply)


# ---- 说话动作 ----
_last_idle = None


def with_idle_motion(reply: str, motions: list[str]) -> str:
    global _last_idle
    if not reply or not motions:
        return reply
    if re.search(r'<\|ACT\s[^|]*"motion"\s*:', reply):  # 角色自己写了动作就听角色的
        return reply
    choices = [m for m in motions if m != _last_idle] if len(motions) > 1 else motions
    _last_idle = random.choice(choices)
    return f"<|ACT {json.dumps({'motion': _last_idle}, ensure_ascii=False, separators=(',', ':'))}|>{reply}"


SILENCE = re.compile(r"^\s*[\[［(（]沉默[\]］)）]\s*$")


def speakable(raw, idle_motions: list[str], outfit_motions: bool = True) -> str:
    """角色的原始回复 → 桌宠念的文字；`|||` 是分条写法，当成一句结束"""
    if not raw or SILENCE.match(str(raw)):
        return ""
    text = re.sub(r"\s*\|\|\|\s*", "。\n", str(raw))
    return with_idle_motion(with_outfit_motions(clean_for_speech(text), outfit_motions), idle_motions)
