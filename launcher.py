"""Start the app, recover incomplete setup, and open only a verified local service."""
import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from app import __version__
from app.startup import APP_ID, data_identity, matches, read_health, wait_ready

ROOT = Path(__file__).resolve().parent
IMPORT_CHECK = 'import fastapi,uvicorn,requests,networkx,bibtexparser,fitz,sklearn'


def ensure_runtime(data_dir):
    explicit = os.environ.get('PAPERNET_PYTHON')
    runtime = Path(explicit) if explicit else ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    log = data_dir / 'private' / 'setup.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    if not runtime.is_file():
        if explicit:
            raise RuntimeError('指定的Python运行环境不存在，请重新配置快捷方式。')
        with log.open('a', encoding='utf-8') as output:
            result = subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], stdout=output, stderr=output, creationflags=flags)
        if result.returncode:
            raise RuntimeError('Python运行环境创建失败，请查看本机安装日志。')
    checked = subprocess.run([str(runtime), '-c', IMPORT_CHECK], capture_output=True, creationflags=flags)
    if checked.returncode:
        if explicit:
            raise RuntimeError('指定的Python运行环境缺少依赖，请修复后重试。')
        print('首次使用正在安装依赖，请保持联网并等待完成。', flush=True)
        with log.open('a', encoding='utf-8') as output:
            result = subprocess.run([str(runtime), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], cwd=ROOT, stdout=output, stderr=output, creationflags=flags)
        if result.returncode:
            raise RuntimeError('依赖安装未完成。请检查网络后再次启动，软件会重新检查并继续安装。')
        checked = subprocess.run([str(runtime), '-c', IMPORT_CHECK], capture_output=True, creationflags=flags)
        if checked.returncode:
            raise RuntimeError('依赖安装后仍不可用，请查看本机安装日志。')
    return runtime


@contextlib.contextmanager
def startup_lock(data_dir):
    path = data_dir / 'private' / 'launcher.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if not path.stat().st_size:
            handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('文献罗盘正在启动，请稍等页面自动打开。')
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def available(port):
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def existing(data_dir):
    try:
        state = json.loads((data_dir / 'private' / 'server.local.json').read_text(encoding='utf-8'))
        port = state.get('port')
        if type(port) is int and 1024 <= port <= 65535:
            url = f'http://127.0.0.1:{port}'
            health = read_health(url)
            if health and health.get('data_id') == data_identity(data_dir) and health.get('app_id') == APP_ID:
                if health.get('version') != __version__:
                    raise RuntimeError('旧版本文献罗盘仍在运行，请先关闭旧服务后重新启动。')
                if matches(health, data_dir, __version__):
                    return url
    except (OSError, ValueError, TypeError):
        pass


def launch(no_browser=False):
    if sys.version_info < (3, 11):
        raise RuntimeError('请先安装Python 3.11或更新版本，再启动文献罗盘。')
    data_dir = Path(os.environ.get('PAPERNET_DATA_DIR') or ROOT / 'data').resolve()
    with startup_lock(data_dir):
        url = existing(data_dir)
        if not url:
            try:
                preferred = int(os.environ.get('PAPERNET_PORT', '8765'))
            except ValueError:
                raise RuntimeError('启动端口设置无效，请使用1024至65535的整数。')
            if not 1024 <= preferred <= 65535:
                raise RuntimeError('启动端口应在1024至65535之间。')
            port = None
            for candidate in range(preferred, min(preferred + 20, 65536)):
                address = f'http://127.0.0.1:{candidate}'
                if available(candidate):
                    port = candidate;break
                health = read_health(address)
                if (health and health.get('app_id') == APP_ID and health.get('data_id') == data_identity(data_dir)
                        and health.get('version') != __version__):
                    raise RuntimeError('旧版本文献罗盘仍在运行，请先关闭旧服务后重新启动。')
                if matches(health, data_dir, __version__):
                    url = address;break
            if not url:
                if port is None:
                    raise RuntimeError('附近端口均被占用，请关闭其他文献罗盘服务后重试。')
                runtime = ensure_runtime(data_dir)
                env = dict(os.environ, PAPERNET_DATA_DIR=str(data_dir), PAPERNET_PORT=str(port), PAPERNET_NO_BROWSER='1', PYTHONUNBUFFERED='1')
                log = data_dir / 'private' / 'startup.log'
                flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
                with log.open('a', encoding='utf-8') as output:
                    process = subprocess.Popen([str(runtime), '-m', 'app.main'], cwd=ROOT, env=env, stdout=output, stderr=output, stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=os.name != 'nt')
                url = f'http://127.0.0.1:{port}'
                try:
                    wait_ready(url, data_dir, __version__, process=process)
                except Exception:
                    if process.poll() is None:
                        process.terminate()
                    raise
                from app.private_settings import atomic_json
                atomic_json(data_dir / 'private' / 'server.local.json', {'port':port,'pid':process.pid,'data_id':data_identity(data_dir),'version':__version__})
        print('文献罗盘已就绪：' + url, flush=True)
        if not no_browser:
            import webbrowser
            if not webbrowser.open(url):
                raise RuntimeError('服务已启动，但浏览器未能自动打开。请复制启动日志中的本机地址打开。')
        return url


def main():
    parser = argparse.ArgumentParser(description='启动文献罗盘')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    try:
        launch(args.no_browser)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else '启动遇到异常，请查看本机启动日志。'
        print(message, file=sys.stderr)
        if os.name == 'nt' and not args.no_browser:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, '文献罗盘启动提示', 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
