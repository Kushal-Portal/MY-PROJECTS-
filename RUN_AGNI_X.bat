@echo off
setlocal
cd /d "%~dp0"
python agni_x.py
if errorlevel 1 (
    echo.
    echo AGNI-X stopped with an error. Check the message above and agni.log.
    pause
)
