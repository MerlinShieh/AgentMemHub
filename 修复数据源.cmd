@echo off
chcp 65001 >nul
cd /d "%~dp0"
python repair_sources.py
pause
