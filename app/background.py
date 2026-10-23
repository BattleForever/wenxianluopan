"""Independent background lifecycle; a hidden window is not process detachment."""
import os
import subprocess


def options():
    if os.name == 'nt':
        # A launcher can itself belong to a terminal/automation kill-on-close job.
        # NEW_PROCESS_GROUP alone does not detach the server from that job.
        return {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x01000000,
                'start_new_session': False}
    return {'creationflags': 0, 'start_new_session': True}


def spawn(args, **kwargs):
    try:
        return subprocess.Popen(args, **kwargs, **options())
    except PermissionError as error:
        if os.name == 'nt' and error.winerror == 5:
            raise RuntimeError('当前启动环境限制独立后台运行。请从桌面“文献罗盘”图标或资源管理器中的start.bat启动。') from error
        raise
