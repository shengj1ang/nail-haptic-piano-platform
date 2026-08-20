@echo off
setlocal
set PATH=%~dp0runtime\Python311;%~dp0runtime\Python311\Scripts;%PATH%
rem Bundled command-line programs the Tools section shells out to
rem (ffmpeg.exe, 7z.exe). Drop them in runtime\bin and they are found
rem without being installed on this machine; setlocal above keeps this
rem PATH inside this window only. app\tool_binaries.py looks in the
rem same folder, so it also works when launcher.py is started directly.
set PATH=%~dp0runtime\bin;%PATH%
echo ===  Entered virtual environment ===

echo  === Loading user interface. ===

python launcher.py

pause
exi