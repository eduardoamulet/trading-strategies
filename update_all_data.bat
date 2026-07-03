@echo off
REM SignalForge - actualiza la cache de datos (Polygon) de TODOS los tickers hasta hoy.
REM Lo ejecuta la Tarea Programada "SignalForge Update Data" cada manana (05:00).
REM Para mas liviano (solo subyacente, sin opciones) cambia los flags por: --underlying-only
cd /d "C:\Users\ROG ZEPHYRUS\OneDrive\Documents\Claude AI\Traiding\options_replay"
"C:\Users\ROG ZEPHYRUS\AppData\Local\Python\pythoncore-3.14-64\python.exe" update_all.py --days 5 --strikes 10 >> "..\_update_all.log" 2>&1
REM data_cache (barras 15m/1H/1D de ALPACA, lado optimizer/Pine): actualizacion INCREMENTAL.
REM Estaba congelado en 2026-04-22 (end hardcodeado + nunca agendado); reactivado 2026-07-03.
cd /d "C:\Users\ROG ZEPHYRUS\OneDrive\Documents\Claude AI\Traiding"
"C:\Users\ROG ZEPHYRUS\AppData\Local\Python\pythoncore-3.14-64\python.exe" -m optimizer.data --update-all >> "_update_all.log" 2>&1
REM Playbook INCREMENTAL: backtestea SOLO los dias nuevos (480 escenarios, ~20 s/dia), los
REM acumula en data/bt_results.db y recalcula el veredicto (ventana 60d + decaimiento).
cd /d "C:\Users\ROG ZEPHYRUS\OneDrive\Documents\Claude AI\Traiding\options_replay"
"C:\Users\ROG ZEPHYRUS\AppData\Local\Python\pythoncore-3.14-64\python.exe" update_playbook.py >> "..\_update_all.log" 2>&1
