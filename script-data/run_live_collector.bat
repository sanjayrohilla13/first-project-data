@echo off
REM Keeps the live data collector running: restarts it 30 seconds after any exit (crash, network error, reboot of the script).
REM Close the window or press Ctrl+C twice to stop. Run  python script-data\live_collector.py --login  once a day BEFORE the market opens
REM At start it checks the Kite token and asks for the login in this window when needed, then backfills, then runs live.
cd /d "%~dp0.."
if not exist logs mkdir logs
:loop
python script-data\live_collector.py %*
echo [%date% %time%] live_collector exited with code %errorlevel%, restarting in 30 seconds... >> logs\live_collector_restarts.log
timeout /t 30 /nobreak >nul
goto loop
