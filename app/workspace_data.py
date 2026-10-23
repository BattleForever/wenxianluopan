"""Private workspace transfers. Consistent snapshots, portable paths, no credentials in exports."""
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from .config import BASE_DIR, DATA_DIR, DB_PATH, PORT
from .private_settings import atomic_json
from .startup import data_identity
from .background import spawn
from . import project_files
from typing import Literal

router = APIRouter(prefix='/api/workspace')
TOKEN = secrets.token_urlsafe(32)
BUSY = threading.Event()
LOCK = threading.Lock()
MAX_BYTES = 4 * 1024**3
FORMAT = 'wenxianluopan-workspace-1'


@contextlib.contextmanager
def database(path, **kwargs):
    connection = sqlite3.connect(path, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def authorize(request):
    host = request.url.hostname
    origin = request.headers.get('origin')
    if (host not in ('127.0.0.1', 'localhost', 'testserver')
            or (origin and origin != str(request.base_url).rstrip('/'))
            or not secrets.compare_digest(request.headers.get('x-workspace-token', ''), TOKEN)):
        raise HTTPException(403, '请从软件页面操作工作空间。')


def job_count():
    count = 0
    with database(DB_PATH) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ('research_jobs', 'enrichment_jobs', 'metric_jobs'):
            if name in tables:
                for (payload,) in db.execute(f'SELECT payload FROM {name}'):
                    if json.loads(payload).get('status') in ('queued', 'running', 'cancelling'):
                        count += 1
        if 'pdf_guide_jobs' in tables:
            count += db.execute("SELECT count(*) FROM pdf_guide_jobs WHERE status='generating'").fetchone()[0]
    return count


@contextlib.contextmanager
def exclusive(request):
    if not LOCK.acquire(False):
        raise HTTPException(409, '工作空间正在处理，请稍候。')
    BUSY.set()
    try:
        # New API requests are blocked by middleware before this check. Existing
        # requests must finish, including synchronous generation/synchronization.
        if job_count() or getattr(request.app.state, 'workspace_requests', 0) > 1:
            raise HTTPException(409, '还有任务正在运行，请等待完成后重试。')
        from . import trash
        # The hourly recycler runs without HTTP. Serialize it with the snapshot.
        with trash._lock:
            yield
    finally:
        BUSY.clear()
        LOCK.release()


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inspect_db(path):
    with database(path.as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('数据库完整性检查未通过。')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'papers', 'citations', 'settings'}.issubset(tables):
            raise ValueError('这不是文献罗盘工作空间。')
        return {'papers': db.execute('SELECT count(*) FROM papers').fetchone()[0],
                'reading': db.execute('SELECT count(*) FROM reading_list').fetchone()[0] if 'reading_list' in tables else 0}


def snapshot(target, portable=True):
    """Only known asset directories; never follow directory symlinks or copy credentials."""
    target.mkdir(parents=True, exist_ok=True)
    with database(DB_PATH) as src, database(target/'papernet.db') as out:
        src.backup(out)
    allowed = ['pdfs', 'private/pdf_sources', 'recycle_staging', '项目文件/个人资料', '项目文件/导出']
    if not portable:
        # Local migration retains connection settings, never temporary locks/logs.
        allowed += ['private', '项目文件']
    copied = set()
    for name in allowed:
        folder = DATA_DIR/name
        if not folder.is_dir() or folder.is_symlink():
            continue
        for path in folder.rglob('*'):
            if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents if p != DATA_DIR):
                continue
            rel = path.relative_to(DATA_DIR)
            if rel in copied or path.suffix in ('.log', '.tmp') or path.name in ('launcher.lock', 'server.local.json', 'relocation.local.json'):
                continue
            if portable and name == 'private':
                continue
            dest = target/rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied.add(rel)
    missing, linked = [], 0
    with database(target/'papernet.db') as db:
        for pid, value in db.execute('SELECT id,local_pdf_path FROM papers WHERE local_pdf_path IS NOT NULL').fetchall():
            if not value:
                continue
            src = Path(value)
            if not src.is_absolute():
                src = DATA_DIR/src
            if not src.is_file() or src.suffix.lower() != '.pdf':
                missing.append(pid)
                db.execute('UPDATE papers SET local_pdf_path=NULL WHERE id=?', (pid,))
                continue
            dest = target/'pdfs'/('paper-' + str(pid) + '-' + digest(src)[:16] + '.pdf')
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            db.execute('UPDATE papers SET local_pdf_path=? WHERE id=?', (dest.relative_to(target).as_posix(), pid))
            linked += 1
        if portable:
            # Fetch responses may retain request URLs containing data-source keys.
            db.execute('DELETE FROM fetch_cache')
            # Old settings rows occasionally contain private machine locations.
            db.execute("DELETE FROM settings WHERE lower(key) LIKE '%key%' OR lower(key) LIKE '%token%' OR lower(key) LIKE '%secret%' OR lower(key) LIKE '%path%' OR lower(key) LIKE '%dir%'")
        db.commit()
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('VACUUM')
    info = inspect_db(target/'papernet.db')
    info.update(linked_pdfs=linked, missing_pdf_ids=missing, includes_connections=not portable)
    if portable and sum(f.stat().st_size for f in target.rglob('*') if f.is_file()) > MAX_BYTES:
        raise ValueError('工作空间超过 4 GB，请先精简附件，或使用目录迁移。')
    files = {f.relative_to(target).as_posix(): digest(f) for f in target.rglob('*') if f.is_file()}
    atomic_json(target/'workspace.json', {'format': FORMAT, 'info': info, 'files': files})
    return info


def target_directory(value):
    if not value.strip() or not Path(value).is_absolute():
        raise ValueError('请选择绝对路径，并使用一个新的空文件夹。')
    path = Path(value).resolve()
    current = DATA_DIR.resolve()
    if path == current or path in current.parents or current in path.parents:
        raise ValueError('新目录不能与当前工作空间重叠。')
    if path == BASE_DIR.resolve() or path in BASE_DIR.resolve().parents:
        raise ValueError('不能把程序目录作为数据目录。')
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('目标目录不是空文件夹；请另选目录，现有文件不会被覆盖。')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def unpack(archive, target):
    """Verify the complete manifest before accepting any imported database."""
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        names = [e.filename for e in entries]
        if len(entries) > 50000 or len(names) != len(set(names)) or sum(e.file_size for e in entries) > MAX_BYTES:
            raise ValueError('备份体积过大或存在重复文件。')
        if len({PurePosixPath(n).as_posix().casefold() for n in names}) != len(names):
            raise ValueError('备份含大小写冲突的文件名。')
        for e in entries:
            p = PurePosixPath(e.filename)
            if (p.is_absolute() or '..' in p.parts or '\\' in e.filename or ':' in e.filename
                    or p.as_posix() != e.filename or any(part.rstrip(' .') != part for part in p.parts)
                    or (e.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('备份含不安全的文件路径。')
            if e.filename not in ('workspace.json', 'papernet.db') and not e.filename.startswith(('pdfs/', 'private/pdf_sources/', 'recycle_staging/', '项目文件/个人资料/', '项目文件/导出/')):
                raise ValueError('备份包含不支持的文件或连接配置。')
        if 'workspace.json' not in names:
            raise ValueError('备份缺少工作空间清单。')
        if z.getinfo('workspace.json').file_size > 8 * 1024**2:
            raise ValueError('备份清单过大。')
        meta = json.loads(z.read('workspace.json'))
        if meta.get('format') != FORMAT or not isinstance(meta.get('files'), dict):
            raise ValueError('请选择文献罗盘生成的工作空间备份。')
        if set(names) != set(meta['files']) | {'workspace.json'} or 'papernet.db' not in meta['files']:
            raise ValueError('备份文件清单不一致。')
        for e in entries:
            path = target/e.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            with z.open(e) as src, path.open('wb') as dst:
                shutil.copyfileobj(src, dst)
            if e.filename != 'workspace.json' and digest(path) != meta['files'][e.filename]:
                raise ValueError('备份校验失败，当前数据未改动。')
        # Never restore connection credentials or startup pointers from a ZIP.
        for name in names:
            if name.startswith('private/') and not name.startswith('private/pdf_sources/'):
                raise ValueError('备份包含不支持的私有配置。')
        return inspect_db(target/'papernet.db')


def relocate_paths(folder):
    with database(folder/'papernet.db') as db:
        for pid, value in db.execute('SELECT id,local_pdf_path FROM papers WHERE local_pdf_path IS NOT NULL').fetchall():
            if not value:
                continue
            rel = PurePosixPath(value)
            if rel.is_absolute() or '..' in rel.parts or '\\' in value or ':' in value:
                raise ValueError('备份中的原文路径不安全。')
            file = folder/value
            if not file.is_file() or file.suffix.lower() != '.pdf':
                raise ValueError('备份缺少关联原文。')
            db.execute('UPDATE papers SET local_pdf_path=? WHERE id=?', (str(file), pid))


class Destination(BaseModel):
    path: str = Field(min_length=1, max_length=2048)


@router.get('/state')
def state(request: Request):
    if request.url.hostname not in ('127.0.0.1', 'localhost', 'testserver'):
        raise HTTPException(403, '请从本机页面打开工作空间。')
    return {'directory': str(DATA_DIR.resolve()), 'token': TOKEN, 'busy': BUSY.is_set(),
            'locations':project_files.locations(),
            'can_exit': callable(getattr(request.app.state, 'stop_server', None)), 'jobs': job_count()}


class FolderTarget(BaseModel):
    kind: Literal['project','papers','originals','backups','exports','notes','system']='project'


@router.post('/open-folder')
def open_folder(request: Request, body: FolderTarget | None=None):
    authorize(request)
    kind=body.kind if body else 'project'
    if kind in ('project','papers','originals'):project_files.refresh()
    path=project_files.directory(kind)
    if os.name == 'nt':
        os.startfile(str(path.resolve()))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path.resolve())])
    else:
        subprocess.Popen(['xdg-open', str(path.resolve())])
    return {'ok': True}


@router.post('/refresh-files')
def refresh_files(request: Request):
    authorize(request)
    return project_files.refresh()


@router.post('/choose-folder')
def choose_folder(request: Request):
    authorize(request)
    if os.name != 'nt':
        raise HTTPException(400, '请在下方填写目标目录的绝对路径。')
    script = "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='选择新工作空间的空文件夹'; if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8; Write-Output $d.SelectedPath}"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-WindowStyle', 'Hidden', '-Command', script], capture_output=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    return {'path': result.stdout.decode('utf-8-sig').strip()}


@router.post('/backup')
def backup(request: Request):
    authorize(request)
    root = Path(tempfile.mkdtemp(prefix='wenxianluopan-backup-'))
    try:
        with exclusive(request):
            info = snapshot(root/'workspace')
            with zipfile.ZipFile(root/'backup.zip', 'w', zipfile.ZIP_DEFLATED) as z:
                for path in (root/'workspace').rglob('*'):
                    if path.is_file():
                        z.write(path, path.relative_to(root/'workspace').as_posix())
            import uuid
            saved=project_files.directory('backups')/(time.strftime('文献罗盘备份_%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:5]+'.zip')
            shutil.copy2(root/'backup.zip',saved)
        shutil.rmtree(root)
        return FileResponse(saved, filename=saved.name,
                            headers={'X-Missing-PDFs': str(len(info['missing_pdf_ids']))},
                            )
    except (ValueError, OSError) as error:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(400, str(error))
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def restart(request, destination):
    stop = getattr(request.app.state, 'stop_server', None)
    if not callable(stop):
        raise HTTPException(409, '请使用 start.bat 启动后再切换工作空间。')
    pointer = DATA_DIR/'private'/'relocation.local.json'
    previous = pointer.read_bytes() if pointer.exists() else None
    env = dict(os.environ, PAPERNET_DATA_DIR=str(DATA_DIR.resolve()), PAPERNET_PORT=str(PORT))
    request.app.state.workspace_switching = True
    request.app.state.stop_reason = 'switch_workspace'
    try:
        atomic_json(pointer, {'target': str(destination), 'data_id': data_identity(destination)})
        spawn([sys.executable, str(BASE_DIR/'launcher.py'), '--after-exit', str(PORT), data_identity(DATA_DIR)],
                         cwd=BASE_DIR, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        if previous is None:
            pointer.unlink(missing_ok=True)
        else:
            pointer.write_bytes(previous)
        request.app.state.workspace_switching = False
        raise
    threading.Timer(.8, stop).start()


@router.post('/migrate')
def migrate(request: Request, body: Destination):
    authorize(request)
    if not callable(getattr(request.app.state, 'stop_server', None)):
        raise HTTPException(409, '请使用启动器启动软件后再迁移。')
    try:
        dest = target_directory(body.path)
        with exclusive(request):
            with tempfile.TemporaryDirectory(prefix='.wenxianluopan-', dir=dest.parent) as tmp:
                stage = Path(tmp)/'workspace'
                info = snapshot(stage, portable=False)
                if dest.exists():
                    dest.rmdir()  # Empty only; no existing file is ever replaced.
                stage.rename(dest)
            relocate_paths(dest)
            restart(request, dest)
            request.app.state.workspace_switching = True
        return {'ok': True, 'directory': str(dest), 'info': info, 'restarting': True}
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error))


@router.post('/restore')
async def restore(request: Request, path: str):
    authorize(request)
    if not callable(getattr(request.app.state, 'stop_server', None)):
        raise HTTPException(409, '请使用启动器启动软件后再导入备份。')
    try:
        dest = target_directory(path)
        with exclusive(request):
            with tempfile.TemporaryDirectory(prefix='.wenxianluopan-', dir=dest.parent) as tmp:
                temp = Path(tmp)
                size = 0
                with (temp/'upload.zip').open('wb') as f:
                    async for block in request.stream():
                        size += len(block)
                        if size > MAX_BYTES:
                            raise ValueError('备份超过 4 GB，请先缩减原文附件。')
                        f.write(block)
                stage = temp/'workspace'
                stage.mkdir()
                info = unpack(temp/'upload.zip', stage)
                if dest.exists():
                    dest.rmdir()
                stage.rename(dest)
            relocate_paths(dest)
            restart(request, dest)
            request.app.state.workspace_switching = True
        return {'ok': True, 'directory': str(dest), 'info': info, 'restarting': True}
    except (ValueError, OSError, zipfile.BadZipFile, KeyError, sqlite3.DatabaseError) as error:
        raise HTTPException(400, str(error))


@router.post('/exit')
def exit_app(request: Request):
    authorize(request)
    stop = getattr(request.app.state, 'stop_server', None)
    if not callable(stop):
        raise HTTPException(409, '当前服务不是由启动器管理，请在启动终端按 Ctrl+C 关闭。')
    with exclusive(request):
        request.app.state.workspace_switching = True
        request.app.state.stop_reason = 'user_exit'
        threading.Timer(.6, stop).start()
    return {'ok': True}
