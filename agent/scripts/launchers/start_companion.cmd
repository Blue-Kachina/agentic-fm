@echo off
rem Start the agentic-fm companion server on Windows. Same options as start_companion.ps1:
rem   start_companion.cmd [-Background] [-Status] [-Stop] [-InstallLogonTask] [-RemoveLogonTask]
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_companion.ps1" %*
exit /b %ERRORLEVEL%
