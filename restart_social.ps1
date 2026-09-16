Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*uvicorn*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$r = ([wmiclass]'Win32_Process').Create(
    'cmd /c "cd /d C:\Users\Starc\thc-social && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 < NUL >> social_out.log 2>&1"')
'ReturnValue=' + $r.ReturnValue
