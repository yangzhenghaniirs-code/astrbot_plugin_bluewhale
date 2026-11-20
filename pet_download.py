# 第一次用时自动下载桌宠（从 DSH 版 npm 插件的 pet-download.js 搬过来）
#   插件本身只有几 MB，桌宠程序（AIRI 特别版 + 大肥鱼模型）约 485MB，放在 Gitee / GitHub 的发行版里
#   顺序：先 Gitee（国内快，6 个分卷），不行再 GitHub（整包）；下完核对 SHA-256，解压出 桌宠\ 放到
#   AstrBot 数据目录\plugin_data\astrbot_plugin_bluewhale\pet-<版本>\
#   下到一半断了：已经下好的分卷下次接着用；整包对不上就删掉重下
import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import aiohttp

from astrbot.api import logger

TAG = "[blue whale]"

# 要下的桌宠版本。换了桌宠就改这里（大小、指纹、地址都要一起改）
PET = {
    "version": "0.4.1",
    "size": 508034601,
    "sha256": "85310ae4863ab1f4df40e3ddd8d32cbeb25cd49d8055e9ff204ed99915c78d42",
    "inner": Path("blue-whale") / "桌宠",  # 包里桌宠所在的文件夹
    "sources": [
        ("Gitee", [f"https://gitee.com/yangzhenghan/blue-whale/releases/download/v0.4.1/blue-whale-v0.4.1.zip.00{i}" for i in range(1, 7)]),
        ("GitHub", ["https://github.com/yangzhenghaniirs-code/blue-whale/releases/download/v0.4.1/blue-whale-v0.4.1.zip"]),
    ],
}


def pet_dir(data_dir: Path) -> Path:
    return data_dir / f"pet-{PET['version']}"


def pet_exe(data_dir: Path) -> Path:
    return pet_dir(data_dir) / "桌宠" / "airi.exe"


def toast(text: str) -> None:
    """Windows 右下角弹通知（不用装任何东西）；失败就只记日志"""
    logger.info(f"{TAG} {text}")
    if os.name != "nt":
        return
    esc = "".join(f"&#{ord(c)};" if c in "<>&\"'" else c for c in text)
    xml = f'<toast><visual><binding template="ToastGeneric"><text>{esc}</text></binding></visual></toast>'
    script = "; ".join([
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null",
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument",
        "$x.LoadXml('" + xml.replace("'", "''") + "')",
        # 借用 PowerShell 的应用 ID，通知才能显示出来
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($x))",
    ])
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


_running: asyncio.Task | None = None


async def ensure_pet(data_dir: Path) -> Path:
    """已经有桌宠就直接返回路径；没有就下载（同一时间只下一份）"""
    global _running
    exe = pet_exe(data_dir)
    if exe.exists():
        return exe
    if _running is None or _running.done():
        _running = asyncio.create_task(_download(data_dir))
    return await asyncio.shield(_running)


async def _download(data_dir: Path) -> Path:
    work = data_dir / f"download-{PET['version']}"
    work.mkdir(parents=True, exist_ok=True)
    zip_path = work / "pet.zip"
    toast(f"blue whale：第一次使用，正在下载桌宠（约 {round(PET['size'] / 1048576)}MB），下好后会自动出来")

    ok = False
    for name, parts in PET["sources"]:
        try:
            await _fetch_source(name, parts, work, zip_path)
            if await asyncio.to_thread(_sha256, zip_path) != PET["sha256"]:
                raise RuntimeError("文件指纹对不上（下载不完整或被改过）")
            ok = True
            logger.info(f"{TAG} 桌宠从 {name} 下载完成")
            break
        except Exception as e:
            logger.warning(f"{TAG} 从 {name} 下载桌宠失败：{e}")
            zip_path.unlink(missing_ok=True)
    if not ok:
        toast("blue whale：桌宠下载失败，请检查网络后重启 AstrBot 再试；也可以到 Gitee / GitHub 手动下载")
        raise RuntimeError("桌宠下载失败")

    toast("blue whale：桌宠下载好了，正在解压……")
    unpack = work / "unpack"
    shutil.rmtree(unpack, ignore_errors=True)
    unpack.mkdir(parents=True)
    tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    proc = await asyncio.create_subprocess_exec(
        str(tar), "-xf", str(zip_path), "-C", str(unpack),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"解压失败：{err.decode(errors='replace')[:300]}")
    inner = unpack / PET["inner"]
    if not (inner / "airi.exe").exists():
        raise RuntimeError("解压后找不到 airi.exe（可能被杀毒软件删了）")

    target = pet_dir(data_dir)
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    shutil.move(str(inner), str(target / "桌宠"))
    shutil.rmtree(work, ignore_errors=True)
    # 旧版本的桌宠用不到了，删掉省空间（删不掉也不影响新桌宠，比如旧桌宠还开着）
    for old in data_dir.glob("pet-*"):
        if old.name != target.name:
            try:
                shutil.rmtree(old)
            except Exception as e:
                logger.warning(f"{TAG} 删旧桌宠失败（不影响使用）：{old.name} {e}")
    logger.info(f"{TAG} 桌宠已装好：{pet_exe(data_dir)}")
    toast("blue whale：桌宠装好了")
    return pet_exe(data_dir)


async def _fetch_source(name: str, parts: list[str], work: Path, zip_path: Path) -> None:
    """按顺序下每个分卷，拼成 zip；已经下完整的分卷（.done 记着大小）不重下"""
    files = []
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "astrbot-bluewhale"}) as session:
        for i, url in enumerate(parts, 1):
            f = work / f"{name}-{i}.part"
            done = Path(str(f) + ".done")
            complete = done.exists() and f.exists() and f.stat().st_size == int(done.read_text() or -1)
            if not complete:
                done.unlink(missing_ok=True)
                await _fetch_file(session, url, f)
                done.write_text(str(f.stat().st_size))
            files.append(f)
            logger.info(f"{TAG} {name} 第 {i}/{len(parts)} 份下好了")
    await asyncio.to_thread(_concat, files, zip_path)
    size = zip_path.stat().st_size
    if size != PET["size"]:
        raise RuntimeError(f"大小不对：{size}，应该是 {PET['size']}")
    for f in files:
        f.unlink(missing_ok=True)
        Path(str(f) + ".done").unlink(missing_ok=True)


async def _fetch_file(session: aiohttp.ClientSession, url: str, f: Path) -> None:
    async with session.get(url, allow_redirects=True) as res:
        if res.status != 200:
            raise RuntimeError(f"HTTP {res.status}：{url}")
        with open(f, "wb") as out:
            async for chunk in res.content.iter_chunked(1 << 20):
                out.write(chunk)


def _concat(files: list[Path], zip_path: Path) -> None:
    with open(zip_path, "wb") as out:
        for f in files:
            with open(f, "rb") as src:
                shutil.copyfileobj(src, out, 1 << 20)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
