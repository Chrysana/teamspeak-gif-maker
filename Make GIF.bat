@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Drag one or more video/GIF files onto this file to convert them.
rem GIFs are written next to each source file.
if "%~1"=="" (
    echo Drag video or GIF files onto this .bat to convert them to TeamSpeak avatar GIFs.
    echo For more options run:  python "%~dp0make_gif.py" --help
    pause
    exit /b
)
python "%~dp0make_gif.py" --interactive %*
pause
