@echo off
rem Stop the subtitle tool web server: kills whatever process is listening
rem on 127.0.0.1:8765. Handy because the server window runs minimized.
rem ASCII-only on purpose (see the launcher bat).
title subtitle-cli stop
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue; Write-Host ('Stopped process pid ' + $_) } } else { Write-Host 'Nothing is listening on port 8765.' }"
ping -n 3 127.0.0.1 >nul
