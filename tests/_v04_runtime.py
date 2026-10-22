"""v04 A 线测试公共工具：构建（可选打补丁的）应用运行时副本。

共享文件（app/main.py、app/config.py、app/ai_brief.py）本轮不得直接改动，
交付为 patches/*.patch；HTTP 层测试与反例包装器据此在临时目录构建
"基线副本 + 补丁" 的运行时，真实模拟总控合入后的形态。

用法：
    from _v04_runtime import build_runtime
    rt = build_runtime()                # 打补丁后的运行时
    rt = build_runtime(patched=False)   # 基线运行时（用于修复前失败证据）
    rt = build_runtime(app_root=...)    # 从指定 app 根构建（如 baseline_readonly）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TRACK_ROOT = Path(__file__).resolve().parents[1]
PATCHES_DIR = TRACK_ROOT / "patches"
PATCH_NAMES = ("config", "main", "ai_brief")


def build_runtime(dest: Path | None = None, *, patched: bool = True,
                  app_root: Path | None = None) -> Path:
    """复制 app/static/tests 到临时运行时目录并（可选）应用全部补丁。"""
    dest = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="v04_rt_"))
    rt = dest / "runtime"
    if rt.exists():
        shutil.rmtree(rt, ignore_errors=True)
    rt.mkdir(parents=True)
    src_app = Path(app_root) / "app" if app_root else TRACK_ROOT / "app"
    src_static = Path(app_root) / "static" if app_root else TRACK_ROOT / "static"
    shutil.copytree(src_app, rt / "app",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(src_static, rt / "static",
                    ignore=shutil.ignore_patterns("__pycache__"))
    tests_src = TRACK_ROOT / "tests"
    if tests_src.is_dir():
        shutil.copytree(tests_src, rt / "tests",
                        ignore=shutil.ignore_patterns("__pycache__"))
    (rt / "evidence").mkdir(exist_ok=True)
    (rt / "tmp").mkdir(exist_ok=True)
    (rt / "data").mkdir(exist_ok=True)
    if patched:
        if not PATCHES_DIR.is_dir():
            # 总控接缝：integrated 内补丁已由总控直接合入共享文件，
            # 无 patches/ 目录时视为"已合入形态"，直接使用当前源码。
            pass
        else:
            for name in PATCH_NAMES:
                patch = PATCHES_DIR / f"{name}.py.patch"
                if not patch.is_file():
                    raise FileNotFoundError(f"缺少补丁: {patch}")
                subprocess.run(["git", "apply", str(patch)], cwd=rt, check=True,
                               capture_output=True, text=True)
    return rt


def runtime_python() -> str:
    """测试用共享解释器（只调用不修改）。"""
    return sys.executable
