@echo off
rem 文献罗盘（WenxianLuopan）- local startup script
rem 历史内部兼容标识 PAPERNET_* 环境变量与 papernet.db 文件名保持不变（技术兼容层，不面向用户）
cd /d %~dp0

if not exist .venv (
  echo Creating a Python virtual environment on first run...
  py -3 -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)

set PAPERNET_NO_BROWSER=
.venv\Scripts\python -m app.main
pause
