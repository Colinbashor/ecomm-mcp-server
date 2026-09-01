@echo off
REM ============================================================
REM  Team-facing warehouse MCP server (HTTP mode). Point a Windows
REM  Task Scheduler task ("At startup", running whether or not
REM  anyone is logged on) at this script to keep the server up
REM  across reboots. Coworkers connect from Claude Desktop using
REM  the URL and config described in SHARING.md.
REM  --host 0.0.0.0 is REQUIRED here and is deliberately not a default:
REM  plain `server.py --http` binds 127.0.0.1, which would leave this
REM  "team-facing" service reachable by nobody but this machine. Narrow it
REM  to a specific interface IP if the wildcard picks up a VPN or hotspot.
REM  Restarts itself if it ever crashes. NB: the backoff uses ping,
REM  not timeout -- timeout errors in a non-interactive session 0
REM  ("input redirection is not supported") and would busy-loop.
REM ============================================================
cd /d "%~dp0"
:loop
.venv\Scripts\python.exe server.py --http --host 0.0.0.0 >> mcp_server_log.txt 2>&1
echo %DATE% %TIME% server exited, restarting in 10s >> mcp_server_log.txt
ping -n 11 127.0.0.1 >nul
goto loop
