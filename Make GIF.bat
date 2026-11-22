@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Opens the TeamSpeak GIF Maker interface. Files dropped onto this .bat are
rem added to it; you can also drag files onto the window once it's open.
title TeamSpeak GIF Maker

rem Use the bundled Python from the release zip if present, else the system one.
set "PY=python"
if exist "%~dp0bin\python\python.exe" set "PY=%~dp0bin\python\python.exe"

"%PY%" "%~dp0tui.py" %*
if errorlevel 1 pause
