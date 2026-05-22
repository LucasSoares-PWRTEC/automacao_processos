@echo off
setlocal
cd /d "%~dp0"
start "" pythonw organizar_processos.py
if errorlevel 1 (
    start "" python organizar_processos.py
)
endlocal
