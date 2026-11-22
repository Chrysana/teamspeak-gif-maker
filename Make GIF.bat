@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Drag one or more video/GIF files onto this file to convert them.
rem GIFs are written next to each source file.

rem Use the bundled Python from the release zip if present, else the system one.
set "PY=python"
if exist "%~dp0bin\python\python.exe" set "PY=%~dp0bin\python\python.exe"

if "%~1"=="" (
    echo Drag video or GIF files onto this .bat to convert them to TeamSpeak avatar GIFs.
    echo For more options run:  "%PY%" "%~dp0make_gif.py" --help
    pause
    exit /b
)
"%PY%" "%~dp0make_gif.py" --interactive %*
pause
