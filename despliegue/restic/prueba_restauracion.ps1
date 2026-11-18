# Prueba de restauración: restaura la última copia en una base VACÍA y temporal y compara recuentos.
# No toca la base de producción. Uso:  .\restic\prueba_restauracion.ps1
$ErrorActionPreference = "Stop"
# restic de winget: si no hay alias "restic" en el PATH, usar el ejecutable del paquete
if (-not (Get-Command restic -ErrorAction SilentlyContinue)) {
    $exe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\restic.restic_*\restic*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($exe) { Set-Alias restic $exe.FullName } else { throw "No encuentro restic: winget install restic.restic" }
}
$aqui = Split-Path -Parent $MyInvocation.MyCommand.Path
Get-Content "$aqui\restic.env" | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}
$dir = Join-Path $env:TEMP "mi-diabetes-restauracion"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dir
New-Item -ItemType Directory $dir | Out-Null
# "restic dump" saca cada archivo sin recrear C:\Users\... (restore copiaría también los permisos de esas carpetas)
$instantanea = restic snapshots latest --tag mi-diabetes --json | ConvertFrom-Json | Select-Object -First 1
foreach ($ruta in $instantanea.paths) {
    $enRepo = "/" + ($ruta -replace ':', '' -replace '\\', '/')
    restic dump $instantanea.short_id $enRepo --target (Join-Path $dir (Split-Path -Leaf $ruta))
    if ($LASTEXITCODE -ne 0) { throw "restic dump falló para $enRepo" }
}
$dump = Get-ChildItem $dir -Filter *.dump | Select-Object -First 1
$esperado = Get-Content "$($dump.FullName).recuentos.txt"

# (con "Stop", Windows PowerShell 5.1 corta el script si un programa escribe en stderr aunque se redirija)
$ErrorActionPreference = "Continue"; docker rm -f md-restauracion *> $null; $ErrorActionPreference = "Stop"
docker run -d --name md-restauracion -e POSTGRES_PASSWORD=temporal timescale/timescaledb:2.17.2-pg16 | Out-Null
try {
    do { Start-Sleep 2 } until ((docker exec md-restauracion pg_isready -U postgres) -match "accepting")
    Start-Sleep 3
    docker cp $dump.FullName md-restauracion:/tmp/copia.dump
    docker exec md-restauracion psql -U postgres -c "CREATE DATABASE restaurada" | Out-Null
    docker exec md-restauracion psql -U postgres -d restaurada -c "SELECT timescaledb_pre_restore()" | Out-Null
    docker exec md-restauracion pg_restore -U postgres -d restaurada --no-owner /tmp/copia.dump
    docker exec md-restauracion psql -U postgres -d restaurada -c "SELECT timescaledb_post_restore()" | Out-Null
    $obtenido = docker exec md-restauracion psql -U postgres -d restaurada -At -c "SELECT 'glucosa', count(*) FROM glucosa UNION ALL SELECT 'dosis', count(*) FROM dosis UNION ALL SELECT 'comidas', count(*) FROM comidas"
    Write-Host "Copia:       $($esperado -join ', ')"
    Write-Host "Restaurada:  $($obtenido -join ', ')"
    if (($esperado -join ";") -eq ($obtenido -join ";")) { Write-Host "OK: los recuentos coinciden" -ForegroundColor Green }
    else { Write-Host "ERROR: los recuentos no coinciden" -ForegroundColor Red; exit 1 }
} finally {
    docker rm -f md-restauracion | Out-Null
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dir
}
