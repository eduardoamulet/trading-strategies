@echo off
rem Shadow trader (Fase 1) — lo invocan las tareas programadas:
rem   "SignalForge Shadow decision" (09:31 ET, lun-vie)  → sin argumentos
rem   "SignalForge Shadow EOD"      (15:50 ET, lun-vie)  → --eod
cd /d "%~dp0"
py shadow_trader.py %*
