"""Disposable working copies inherit the application's own directory access."""
from contextlib import contextmanager
import logging,re,shutil,time,uuid
from .config import DATA_DIR

log=logging.getLogger('papernet.work')

def cleanup(folder,root):
    # Only remove our generated immediate child; never follow a replaced link.
    if folder.is_symlink() or folder.resolve().parent!=root.resolve() or not re.fullmatch(r'index-[a-f0-9]{32}',folder.name):return
    try:shutil.rmtree(folder)
    except OSError:log.warning('temporary_index_cleanup_deferred')

@contextmanager
def index_copy():
    root=DATA_DIR/'private'/'work';root.mkdir(parents=True,exist_ok=True)
    # Retry old, generated copies on later use. Active requests are not touched.
    for old in root.glob('index-*'):
        try:
            if time.time()-old.stat().st_mtime>3600:cleanup(old,root)
        except OSError:pass
    folder=root/('index-'+uuid.uuid4().hex);folder.mkdir()
    try:yield folder
    finally:cleanup(folder,root)
