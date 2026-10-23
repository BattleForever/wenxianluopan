"""Local service identity and readiness checks, using only the standard library."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_ID = 'wenxianluopan'


def data_identity(path):
    canonical = os.path.normcase(str(Path(path).resolve()))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]


def read_health(url):
    try:
        # Local startup must not follow proxy settings or redirects to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url.rstrip('/') + '/api/health', timeout=1.5) as response:
            value = json.loads(response.read(65536))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def matches(health, data_dir, version):
    return bool(health and health.get('ok') is True and health.get('app_id') == APP_ID
                and health.get('data_id') == data_identity(data_dir)
                and health.get('version') == version)


def wait_ready(url, data_dir, version, timeout=60, process=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError('服务未能启动，请查看本机启动日志。')
        if matches(read_health(url), data_dir, version):
            return True
        time.sleep(.25)
    raise RuntimeError('等待服务就绪超时，请查看本机启动日志后重新启动。')


def open_ready(url, data_dir, version, timeout=60):
    wait_ready(url, data_dir, version, timeout)
    return webbrowser.open(url)
