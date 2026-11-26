@echo off
cd /d "%~dp0"
if not exist warehouse.pid goto none
for /f %%p in (warehouse.pid) do taskkill /F /PID %%p /FI "IMAGENAME eq pythonw.exe" >nul 2>&1
for /f %%p in (warehouse.pid) do taskkill /F /PID %%p /FI "IMAGENAME eq python.exe" >nul 2>&1
del warehouse.pid
echo Clothing Warehouse was stopped.
goto end
:none
echo Clothing Warehouse is not running.
:end
timeout /t 2 >nul
