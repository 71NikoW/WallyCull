@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0einrichten.ps1" %*
if errorlevel 1 pause
