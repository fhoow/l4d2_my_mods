@echo off
cd /d "%~dp0"
python rpc_server.py --strategy gemini_decision %*
pause
