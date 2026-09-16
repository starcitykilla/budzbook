$out = @{}
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/age-check' -UseBasicParsing
    $out['gate'] = $r.StatusCode
} catch { $out['gate'] = 'FAIL' }
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing -MaximumRedirection 0 -ErrorAction Stop
    $out['root'] = 'NO_REDIRECT_' + $r.StatusCode
} catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 303) { $out['root'] = 'GATED_303' }
    else { $out['root'] = 'FAIL' }
}
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/stream/avatars' -UseBasicParsing
    $out['api'] = $r.StatusCode
} catch { $out['api'] = 'FAIL' }
$out['gate'] ; $out['root'] ; $out['api']
