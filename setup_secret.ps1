$chars = (48..57) + (65..90) + (97..122)
$secret = -join ($chars | Get-Random -Count 48 | ForEach-Object { [char]$_ })
[Environment]::SetEnvironmentVariable('THC_SESSION_SECRET', $secret, 'Machine')
Write-Output 'SECRET_SET'
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn*' }
if ($procs) {
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Output 'KILLED_OLD'
} else {
    Write-Output 'NONE_RUNNING'
}
