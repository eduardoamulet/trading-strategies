@echo off
REM SignalForge - actualiza la cache de datos (Polygon) de TODOS los tickers hasta hoy.
REM Lo ejecuta la Tarea Programada "SignalForge Update Data" cada manana (05:00).
REM Para mas liviano (solo subyacente, sin opciones) cambia los flags por: --underlying-only
cd /d "C:\Users\ROG ZEPHYRUS\OneDrive\Documents\Claude AI\Traiding\options_replay"
"C:\Users\ROG ZEPHYRUS\AppData\Local\Python\pythoncore-3.14-64\python.exe" update_all.py --days 5 --strikes 10 >> "..\_update_all.log" 2>&1
