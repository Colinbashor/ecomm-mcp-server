@echo off
REM ============================================================
REM  Team-facing warehouse MCP server (HTTP mode). Point a Windows
REM  Task Scheduler task ("At startup", running whether or not
REM  anyone is logged on) at this script to keep the server up
REM  across reboots. Coworkers connect from Claude Desktop using
REM  the URL and config described in SHARING.md.
REM  Restarts itself if it ever crashes. NB: the backoff uses ping,
REM  not timeout -- timeout errors in a non-interactive session 0
REM  ("input redirection is not supported") and would busy-loop.
REM ============================================================
cd /d "%~dp0"
:loop
.venv\Scripts\python.exe server.py --http >> mcp_server_log.txt 2>&1
echo %DATE% %TIME% server exited, restarting in 10s >> mcp_server_log.txt
ping -n 11 127.0.0.1 >nul
goto loop
