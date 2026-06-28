@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYW=C:\Program Files\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" trae_traffic_light.py
