import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# parallel_v04 §3.4 数据路径：新增环境变量 PAPERNET_DATA_DIR（数据目录，
# DB_PATH=其下 papernet.db），与既有 PAPERNET_AI_CONFIG_FILE 并列。
# 未设置时保持历史默认行为（代码目录 data/），全部历史测试兼容。
# 长期库用法：设置 PAPERNET_DATA_DIR 指向长期库目录（内含 papernet.db）
# 后启动，应用直接使用长期库 papernet.db（见 evidence/数据路径说明.md）。
_env_data_dir = os.environ.get("PAPERNET_DATA_DIR")
DATA_DIR = Path(_env_data_dir) if _env_data_dir else BASE_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"
DB_PATH = DATA_DIR / "papernet.db"
STATIC_DIR = BASE_DIR / "static"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

HOST = "127.0.0.1"
PORT = int(os.environ.get("PAPERNET_PORT", "8765"))

# 出站学术 API 白名单（SSRF 防护）：仅允许这些固定 https 主机
ALLOWED_HOSTS = {
    "api.openalex.org",
    "opencitations.net",
    "api.semanticscholar.org",
    "api.crossref.org",
    "doi.org",
    "www.easyscholar.cc",
    "api.easyscholar.cc",
}
# OA PDF 下载额外允许的公认开放获取仓储（仅用于"下载 OA PDF"功能）
PDF_EXTRA_HOSTS = {
    "arxiv.org",
    "export.arxiv.org",
    "pmc.ncbi.nlm.nih.gov",
    "www.ncbi.nlm.nih.gov",
    "core.ac.uk",
    "www.biorxiv.org",
    "www.medrxiv.org",
}
# Zotero 本地集成是唯一例外：固定基址、只读、需用户在设置中启用
ZOTERO_BASE = "http://127.0.0.1:23119"
ZOTERO_ITEM_KEY_RE = r"^[A-Za-z0-9]+$"

DEFAULT_SETTINGS = {
    "offline_mode": "0",
    "mailto": "",
    "citedby_cap": "300",
    "opencitations_enabled": "1",
    "zotero_enabled": "0",
    "zotero_data_dir": str(Path.home() / "Zotero"),
    "candidate_cap": "300",
    "build_batch_size": "25",
}
