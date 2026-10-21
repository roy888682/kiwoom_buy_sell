@echo off
call "%~dp0start.bat" --stop-background
exit /b %errorlevel%
