# start_social_boot.ps1 - launch BudzBook reading OAuth config from the
# persistent User environment (no secrets on the process command line).
Set-Location C:\Users\Starc\thc-social
$env:TWITCH_CLIENT_ID = ([Environment]::GetEnvironmentVariable('TWITCH_CLIENT_ID','User')).Trim()
$env:TWITCH_CLIENT_SECRET = ([Environment]::GetEnvironmentVariable('TWITCH_CLIENT_SECRET','User')).Trim()
$env:TWITCH_REDIRECT_URI = ([Environment]::GetEnvironmentVariable('TWITCH_REDIRECT_URI','User')).Trim()
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> social_out.log 2>&1
