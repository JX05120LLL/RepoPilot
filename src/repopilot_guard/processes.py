"""跨平台子进程启动选项；Windows 桌面端必须保持后台命令无窗口。"""

from __future__ import annotations

import os
import subprocess


def hidden_process_kwargs() -> dict[str, object]:
    """返回适用于 ``subprocess.run/Popen`` 的后台进程参数。

    ``CREATE_NO_WINDOW`` 阻止 Git、Maven 等控制台程序创建窗口；
    ``STARTF_USESHOWWINDOW`` 作为批处理包装器和旧版 Windows 的兼容保护。
    非 Windows 平台返回空字典，不改变原有进程语义。
    """

    if os.name != "nt":
        return {}
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startup_info,
    }
