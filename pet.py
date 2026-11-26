# 桌宠程序（AIRI 特别版）的开关和小修补（从 DSH 版 blue whale 的 pet.js / vad-mirror.js 搬过来）
#   PetLauncher：AstrBot 启动时打开桌宠，关掉时一起关；桌宠已经开着就不重复打开
#   patch_pet：打开桌宠前对 app.asar 做「同样长度原位替换」（比 DSH 版多一条：不显示思考中的三点气泡）
#   ModelServer：本机 18101 提供语音检测模型，国内不用连 huggingface.co
import asyncio
import ctypes
import os
import subprocess
from pathlib import Path

try:  # 只有 Windows 有；别的系统上插件照样能加载，只是不打开桌宠
    from ctypes import wintypes
except Exception:  # pragma: no cover
    wintypes = None

import psutil
from aiohttp import web

from astrbot.api import logger

TAG = "[blue whale]"


# ---------------- 打开 / 关闭桌宠 ----------------
def _clean_env() -> dict:
    # AstrBot 是 Python 程序，这些变量传给 airi.exe（Electron）可能让它当成 Node 启动后马上退出，去掉
    env = dict(os.environ)
    for key in list(env):
        if key.upper().startswith(("ELECTRON_", "PYTHON")) or key.upper() in ("NODE_OPTIONS", "NODE_PATH", "NODE_ENV"):
            env.pop(key, None)
    return env


def _same(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def pet_running(exe: str) -> bool:
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if (p.info["name"] or "").lower() == "airi.exe" and p.info["exe"] and _same(p.info["exe"], exe):
                return True
        except Exception:
            continue
    return False


class _Job:
    """Windows 作业对象：AstrBot 进程没了（包括被强行关掉），系统自动把桌宠一起关掉"""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt" or wintypes is None:
            return
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE

            class LIMIT(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
                ]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

            class EXT(ctypes.Structure):
                _fields_ = [("Basic", LIMIT), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            h = k32.CreateJobObjectW(None, None)
            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if h and k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle, self.k32 = h, k32
        except Exception as e:
            logger.warning(f"{TAG} 建作业对象失败（不影响使用，只是 AstrBot 被强关时桌宠不会跟着关）：{e}")

    def add(self, pid: int) -> None:
        if not self.handle:
            return
        try:
            self.k32.OpenProcess.restype = wintypes.HANDLE
            ph = self.k32.OpenProcess(0x0001 | 0x0100, False, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
            if ph:
                self.k32.AssignProcessToJobObject(self.handle, ph)
                self.k32.CloseHandle(ph)
        except Exception:
            pass


class PetLauncher:
    def __init__(self, exe: str, close_on_exit: bool = True) -> None:
        self.exe = exe
        self.close_on_exit = close_on_exit
        self.proc: subprocess.Popen | None = None
        self.job = _Job() if close_on_exit else None

    def start(self) -> None:
        if not self.exe or not Path(self.exe).exists():
            logger.warning(f"{TAG} 找不到桌宠程序：{self.exe}（插件设置里填 airi.exe 的位置）")
            return
        if pet_running(self.exe):
            logger.info(f"{TAG} 桌宠已经开着了")
            return
        self.proc = subprocess.Popen(
            [self.exe], cwd=str(Path(self.exe).parent), env=_clean_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        if self.job:
            self.job.add(self.proc.pid)
        logger.info(f"{TAG} 已打开桌宠，进程 {self.proc.pid}")

    def stop(self) -> None:
        if not self.proc or not self.close_on_exit:
            return
        try:
            parent = psutil.Process(self.proc.pid)
            for p in parent.children(recursive=True) + [parent]:
                try:
                    p.kill()
                except Exception:
                    pass
            logger.info(f"{TAG} 已关闭桌宠")
        except psutil.NoSuchProcess:
            pass
        self.proc = None


# ---------------- 打开桌宠前的小修补（和 DSH 版 vad-mirror.js 一样） ----------------
VAD_PORT = 18101
MODEL_FILE = Path(__file__).parent / "assets" / "silero-vad.onnx"
MODEL_PATH = "/onnx-community/silero-vad/resolve/main/onnx/model.onnx"

BORDER_OLD = "const nearBorder = isAroundWindowBorder.value || isAroundWindowBorderFor250Ms.value;"
BORDER_NEW = "const nearBorder = false;"
TRAY_OLD = '\t\t\t{\n\t\t\t\tlabel: "显示 / 隐藏桌宠",\n\t\t\t\tclick: () => toggleWindowShow(params.mainWindow)\n\t\t\t}'
TRAY_NEW = '{label:"显示/隐藏桌宠",click:(a,b,c,w=params.mainWindow)=>w.isVisible()?w.hide():w.show()}'
CORNER_OLD = (
    "/**\n\t\t* Whether the cursor sits on the stage canvas rather than on interface drawn over it.\n\t\t*\n"
    "\t\t* The pixel test can only answer for the canvas, and the canvas draws nothing beneath a\n"
    "\t\t* DOM overlay, so a button, a toast or a portaled panel floating over blank canvas\n"
    "\t\t* would read as empty space and lose its clicks. Ask the document what is really under\n"
    "\t\t* the cursor instead. This is a hit test, not an event, so it still answers while the\n"
    "\t\t* window is click-through.\n\t\t*/\n"
    "\t\tconst isPointerOverStageCanvas = computed(() => document.elementFromPoint(relativeMouseX.value, relativeMouseY.value) === stageCanvas.value);"
)
CORNER_NEW = (
    "const isPointerOverStageCanvas = computed(() => { const hit = document.elementFromPoint(relativeMouseX.value, "
    "relativeMouseY.value); return hit === stageCanvas.value || !!(hit && stageCanvas.value && hit.contains(stageCanvas.value)); });"
)


# 不要「思考中」的三个点小气泡：桌宠在等回复时，角色头上会冒出一个三点气泡（presence bubble 的 thinking 状态）
#   改成永远显示「空闲」状态，就不会出现；未读数气泡不受影响
THINKING_OLD = "const chatPresence = computed(() => chatSending.value ? presenceBubbleThinking : presenceBubbleIdle);"
THINKING_NEW = "const chatPresence = computed(() => presenceBubbleIdle);"


def _same_length(name: str, old: str, new: str) -> tuple[str, bytes, bytes]:
    f, t = old.encode(), new.encode()
    short = len(f) - len(t)
    if short > 0:  # 短了就在最后一个字符前补空格
        t = (new[:-1] + " " * short + new[-1:]).encode()
    if len(f) != len(t):
        raise ValueError(f"修补「{name}」前后长度不一致")
    return name, f, t


PET_PATCHES = [
    _same_length("语音检测模型地址换成本机", 'remoteHost: "https://huggingface.co/"', f'remoteHost: "http://127.0.0.1:{VAD_PORT}/"'),
    _same_length("屏幕边缘不再抢鼠标", BORDER_OLD, BORDER_NEW),
    _same_length("托盘显示/隐藏能隐藏", TRAY_OLD, TRAY_NEW),
    _same_length("屏幕四个角也点得穿", CORNER_OLD, CORNER_NEW),
    _same_length("不显示思考中的三点气泡", THINKING_OLD, THINKING_NEW),
]


def _find_all(fh, size: int, needles: list[bytes]) -> list[list[int]]:
    longest = max(len(n) for n in needles)
    chunk = 32 * 1024 * 1024
    hits: list[list[int]] = [[] for _ in needles]
    pos = 0
    while pos < size:
        fh.seek(pos)
        view = fh.read(min(chunk + longest - 1, size - pos))
        for k, needle in enumerate(needles):
            i = view.find(needle)
            while i != -1:
                if i < chunk:
                    hits[k].append(pos + i)
                i = view.find(needle, i + 1)
        pos += chunk
    return hits


def patch_pet(airi_exe: str) -> None:
    """打开桌宠前修补；已经改过的跳过。记号文件 .patches-v5（加了新的修补就要换一个版本号，不然会被当成已经修补过）"""
    asar = Path(airi_exe).parent / "resources" / "app.asar"
    if not asar.exists():
        logger.warning(f"{TAG} 找不到桌宠的 app.asar，跳过修补：{asar}")
        return
    mark = Path(str(asar) + ".patches-v5")

    def stamp() -> str:
        st = asar.stat()
        return f"{st.st_size}:{st.st_mtime_ns // 1_000_000}"

    try:
        if mark.read_text(encoding="utf-8") == stamp():
            return
    except Exception:
        pass
    try:
        with open(asar, "r+b") as fh:
            size = asar.stat().st_size
            needles = [b for _, f, t in PET_PATCHES for b in (f, t)]
            hits = _find_all(fh, size, needles)
            for k, (name, _f, t) in enumerate(PET_PATCHES):
                todo, done = hits[k * 2], len(hits[k * 2 + 1])
                for at in todo:
                    fh.seek(at)
                    fh.write(t)
                logger.info(f"{TAG} " + (f"已修补桌宠：{name}（{len(todo)} 处）" if todo else f"桌宠已经修补过：{name}" if done else f"桌宠里没找到要修补的地方，跳过：{name}"))
        mark.write_text(stamp(), encoding="utf-8")
    except Exception as e:
        logger.warning(f"{TAG} 修补桌宠失败（桌宠开着时改不了，关掉桌宠后重启 AstrBot 会再试）：{e}")


# ---------------- 本机语音检测模型 ----------------
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Private-Network": "true",
    "Access-Control-Expose-Headers": "Content-Length, ETag",
}


class ModelServer:
    def __init__(self) -> None:
        self.runner: web.AppRunner | None = None

    async def _handle(self, req: web.Request) -> web.StreamResponse:
        if req.method == "OPTIONS":
            return web.Response(status=204, headers=CORS)
        if req.path == MODEL_PATH and MODEL_FILE.exists():
            if req.method == "HEAD":
                return web.Response(headers={**CORS, "Content-Type": "application/octet-stream",
                                             "Content-Length": str(MODEL_FILE.stat().st_size), "ETag": '"silero-vad-bundled"'})
            return web.FileResponse(MODEL_FILE, headers={**CORS, "Content-Type": "application/octet-stream", "ETag": '"silero-vad-bundled"'})
        return web.Response(status=302, headers={**CORS, "Location": "https://huggingface.co" + req.path_qs})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", VAD_PORT).start()
            logger.info(f"{TAG} 语音检测模型服务已启动：http://127.0.0.1:{VAD_PORT}")
        except OSError as e:
            logger.warning(f"{TAG} 语音检测模型服务没启动（端口 {VAD_PORT} 被占用，可能 DSH 版 blue whale 开着）：{e}")

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


async def prepare_and_start(launcher: PetLauncher) -> None:
    if launcher.exe and Path(launcher.exe).exists() and not pet_running(launcher.exe):
        await asyncio.to_thread(patch_pet, launcher.exe)
    launcher.start()
