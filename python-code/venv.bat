@echo off
setlocal
set PATH=%~dp0runtime\Python311;%~dp0runtime\Python311\Scripts;%PATH%
echo ===  Entered virtual environment ===
echo Python Version:
python --version
echo pip version:
python -m pip --version
echo You should use python -m pip xxx in this virtual environment
echo.
echo You can type command now£¨type'exit' to exit£©...
cmd /k