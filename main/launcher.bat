@echo off
setlocal
set PATH=%~dp0runtime\Python311;%~dp0runtime\Python311\Scripts;%PATH%
echo ===  Entered virtual environment ===

echo  === Loading user interface. ===

python launcher.py

pause
exi