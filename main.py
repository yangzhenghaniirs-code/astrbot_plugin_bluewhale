# blue whale：AstrBot 版的大肥鱼桌宠插件（从 DSH 版 @yangzhenghan/dsh-bluewhale 改过来）
#   1. 注册「blue whale 桌宠」聊天平台（adapter.py），第一次装上自动加进 AstrBot 的平台列表并启动
#   2. AstrBot 启动时打开桌宠、关掉时一起关（pet.py），打开前做和 DSH 版一样的小修补，并在 18101 提供语音检测模型
#   3. 只在和桌宠说话时，把「桌宠规则.md」加进系统提示词：口语短句、表情动作、换装、跳舞的写法
#   4. 发给桌宠的回复不转成图片（AstrBot 的「长文转图」对桌宠没用）
import asyncio
import os
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .adapter import ADAPTER, SHARED, BlueWhaleAdapter  # noqa: F401  导入时注册平台
from .pet import ModelServer, PetLauncher, prepare_and_start
from .pet_download import ensure_pet, pet_exe

TAG = "[blue whale]"
PLUGIN_NAME = "astrbot_plugin_bluewhale"
PLUGIN_DIR = Path(__file__).parent
HOME = Path(os.path.expanduser("~"))
IS_WINDOWS = os.name == "nt"


def find_pet(data_dir: Path) -> Path | None:
    """找桌宠程序：插件自己下载的 → DSH 版 blue whale 已经有的（同一台电脑装过 DSH 版时直接用，不用再下 485MB）"""
    candidates = [
        pet_exe(data_dir),
        HOME / ".dsh" / "plugins" / "dsh-dafei" / "桌宠" / "airi.exe",
        *sorted((HOME / ".dsh" / "blue-whale").glob("pet-*/桌宠/airi.exe"), reverse=True),
    ]
    return next((p for p in candidates if p.exists()), None)

DEFAULT_PLATFORM = {
    "id": ADAPTER,
    "type": ADAPTER,
    "enable": True,
    "port": 18100,
    "session_name": "owner",
    "sender_name": "主人",
}


class BlueWhale(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        # 设置里填了就用填的；没填就自动找，找不到时启动后自动下载（_startup）
        self.custom_exe = (config.get("airi_exe") or "").strip().strip('"')
        found = Path(self.custom_exe) if self.custom_exe else find_pet(self.data_dir)
        self.launcher = PetLauncher(str(found or pet_exe(self.data_dir)), bool(config.get("close_pet_on_exit", True)))
        self.model_server = ModelServer()
        self._tasks: list[asyncio.Task] = []
        SHARED.update(
            context=context,
            idle_motions=list(config.get("idle_motions") or ["Animation/正常说话-*.motion3.json"]),
            outfit_motions=bool(config.get("outfit_motions", True)),
            timeout=int(config.get("timeout_seconds", 180)),
            fallback_reply=config.get("fallback_reply") or "我刚刚走神了，你再说一遍？",
            split_bubbles=bool(config.get("split_bubbles", True)),
            tts_enabled=bool(config.get("tts_enabled", False)),
            tts_api_key=(config.get("tts_api_key") or "").strip(),
            tts_host=(config.get("tts_host") or "").strip(),
            tts_model=(config.get("tts_model") or "cosyvoice-v3.5-flash").strip(),
            tts_voice=(config.get("tts_voice") or "").strip(),
        )

    async def initialize(self) -> None:
        await self.model_server.start()
        self._tasks.append(asyncio.create_task(self._startup()))

    async def _startup(self) -> None:
        try:
            await self._ensure_platform()
            if not self.config.get("auto_launch_pet", True):
                return
            if not IS_WINDOWS:
                logger.warning(f"{TAG} 桌宠程序只支持 Windows，这台电脑不打开桌宠（桌宠接口照常在 18100 端口）")
                return
            if not Path(self.launcher.exe).exists():
                if self.custom_exe:
                    logger.warning(f"{TAG} 设置里填的桌宠程序不存在：{self.custom_exe}")
                    return
                if not self.config.get("auto_download_pet", True):
                    logger.warning(f"{TAG} 没有桌宠程序，自动下载已关闭（插件设置里打开，或填 airi.exe 的位置）")
                    return
                # 第一次使用：下载桌宠（后台进行，不耽误 AstrBot 使用）
                self.launcher.exe = str(await ensure_pet(self.data_dir))
            await prepare_and_start(self.launcher)
        except Exception as e:
            logger.error(f"{TAG} 启动出错：{e}", exc_info=True)

    async def _ensure_platform(self) -> None:
        """平台列表里没有 blue whale 就加上并启动（第一次装插件、或热重载后）"""
        pm = self.context.platform_manager
        # AstrBot 启动时插件比平台先加载：等平台管理器把网页聊天也开好了再看
        for _ in range(120):
            if any(p.meta().name == "webchat" for p in pm.get_insts()):
                break
            await asyncio.sleep(1)
        entry = next((p for p in pm.platforms_config if p.get("type") == ADAPTER), None)
        if entry is None:
            entry = dict(DEFAULT_PLATFORM)
            pm.platforms_config.append(entry)
            pm.astrbot_config.save_config()
            logger.info(f"{TAG} 已把「blue whale 桌宠」加进 AstrBot 的平台列表")
        if not entry.get("enable"):
            logger.info(f"{TAG} 「blue whale 桌宠」平台在 AstrBot 里被关掉了，桌宠连不上（到 平台 里打开）")
            return
        if not any(p.meta().name == ADAPTER for p in pm.get_insts()):
            await pm.load_platform(entry)

    async def terminate(self) -> None:
        for t in self._tasks:
            t.cancel()
        self.launcher.stop()
        await self.model_server.stop()
        # 插件被停用 / 卸载 / 重载：把平台也停掉，空出 18100 端口（重载后会再启动）
        try:
            pm = self.context.platform_manager
            for p in list(pm.get_insts()):
                if p.meta().name == ADAPTER:
                    await pm.terminate_platform(p.meta().id)
        except Exception as e:
            logger.warning(f"{TAG} 停平台时出错：{e}")

    # ---------- 只对桌宠生效的两个钩子 ----------
    def _rules(self) -> str:
        if not self.config.get("attach_rules", True):
            return ""
        custom = (self.config.get("rules_file") or "").strip().strip('"')
        for f in ([Path(custom)] if custom else []) + [StarTools.get_data_dir("astrbot_plugin_bluewhale") / "桌宠规则.md", PLUGIN_DIR / "桌宠规则.md"]:
            try:
                if f.exists():
                    return f.read_text(encoding="utf-8-sig").strip()
            except Exception:
                continue
        return ""

    @filter.on_llm_request()
    async def add_pet_rules(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if event.get_platform_name() != ADAPTER:
            return
        rules = self._rules()
        if rules:
            req.system_prompt = (req.system_prompt or "") + "\n\n# 桌宠当面语音回复约定\n" + rules

    @filter.on_decorating_result()
    async def no_text_to_image(self, event: AstrMessageEvent) -> None:
        if event.get_platform_name() != ADAPTER:
            return
        result = event.get_result()
        if result is not None:
            result.use_t2i_ = False
