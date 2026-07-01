"""本地目录浏览与文件列举（供「本地目录」功能用）。

应用绑定 127.0.0.1，单机单用户，故允许浏览本机文件系统：列盘符、列子目录、列文件。
不做写操作。所有函数对越权/不存在路径返回空或抛 ValueError，由路由转可读错误。

文件按扩展名归类（kind）：image（截图）/ pdf / word / excel / ppt，供前端分类与
后端内容抽取路由。
"""
from __future__ import annotations

import datetime as dt
import os
import string
import sys
from pathlib import Path

_IS_WINDOWS = sys.platform.startswith("win")

# 扩展名 → 分类
KIND_BY_EXT: dict[str, str] = {
    # 截图 / 图片
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".bmp": "image", ".webp": "image",
    # 文档
    ".pdf": "pdf",
    ".doc": "word", ".docx": "word",
    ".xls": "excel", ".xlsx": "excel", ".xlsm": "excel", ".csv": "excel",
    ".ppt": "ppt", ".pptx": "ppt",
}
SUPPORTED_EXTS = set(KIND_BY_EXT)
IMAGE_EXTS = {e for e, k in KIND_BY_EXT.items() if k == "image"}
KIND_LABELS = {"image": "截图", "pdf": "PDF", "word": "Word", "excel": "Excel", "ppt": "PPT"}


def kind_of(name: str) -> str | None:
    return KIND_BY_EXT.get(os.path.splitext(name)[1].lower())


def _drives() -> list[dict]:
    """Windows 盘符列表（C:\\、D:\\…）。非 Windows 返回根目录。"""
    if not _IS_WINDOWS:
        return [{"name": "/", "path": "/"}]
    out: list[dict] = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            out.append({"name": f"{letter}:", "path": root})
    return out


def _file_entry(entry: os.DirEntry) -> dict | None:
    kind = kind_of(entry.name)
    if not kind:
        return None
    st = entry.stat()
    return {
        "name": entry.name, "path": str(Path(entry.path)),
        "size": st.st_size, "kind": kind,
        "mtime": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "mtime_ts": st.st_mtime,
    }


def browse(path: str | None) -> dict:
    """列出目录内容（含子目录 + 受支持文件）。

    path 为空 → 返回盘符（is_root=True）+ 常用入口（桌面/图片/下载）。
    返回 ``{path, parent, is_root, dirs:[{name,path}], files:[...], shortcuts:[...]}``。
    """
    if not path:
        home = Path.home()
        shortcuts = []
        for label, p in [("桌面", home / "Desktop"), ("图片", home / "Pictures"),
                         ("下载", home / "Downloads"), ("用户主目录", home)]:
            if p.is_dir():
                shortcuts.append({"name": label, "path": str(p)})
        return {"path": "", "parent": None, "is_root": True,
                "dirs": _drives(), "files": [], "shortcuts": shortcuts}

    d = Path(path).expanduser()
    if not d.exists() or not d.is_dir():
        raise ValueError("目录不存在或不可访问。")

    dirs: list[dict] = []
    files: list[dict] = []
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    name = e.name
                    if name.startswith("$") or name.startswith("."):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        dirs.append({"name": name, "path": str(Path(e.path))})
                    elif e.is_file(follow_symlinks=False):
                        fe = _file_entry(e)
                        if fe:
                            files.append(fe)
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError) as e:
        raise ValueError(f"无法读取目录：{e}")

    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["mtime_ts"], reverse=True)
    parent = str(d.parent) if d.parent != d else None
    return {"path": str(d), "parent": parent, "is_root": False,
            "dirs": dirs, "files": files, "shortcuts": []}


def list_files(directory: str) -> dict:
    """列目录下所有受支持文件，按修改时间倒序（最新在前），每项带 kind。"""
    d = Path(directory).expanduser()
    if not directory or not d.is_dir():
        raise ValueError("目录不存在或不可访问。")
    items: list[dict] = []
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    fe = _file_entry(e)
                    if fe:
                        items.append(fe)
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError) as e:
        raise ValueError(f"无法读取目录：{e}")
    items.sort(key=lambda x: x["mtime_ts"], reverse=True)
    # 分类计数，供前端筛选器展示
    counts: dict[str, int] = {}
    for it2 in items:
        counts[it2["kind"]] = counts.get(it2["kind"], 0) + 1
    return {"directory": str(d), "items": items, "counts": counts}


def is_image_file(path: str) -> bool:
    p = Path(path)
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS
