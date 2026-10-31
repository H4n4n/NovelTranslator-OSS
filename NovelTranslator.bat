@echo off
rem Starts the NovelTranslator app and opens it in your browser.
rem Closing this window closes the app. A translation already running keeps going.
cd /d "%~dp0"
python -m ui.server --open
if errorlevel 1 pause
