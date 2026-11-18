# Prueba de restauración: restaura la última copia en una base VACÍA y temporal y compara recuentos.
# No toca la base de producción. Uso:  .\restic\prueba_restauracion.ps1
$ErrorActionPreference = "Stop"
$aqui = Split-Path -Parent $MyInvocation.MyCommand.Path
Get-Content "$aqui\restic.env" | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}
$dir = Join-Path $env:TEMP "mi-diabetes-restauracion"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dir
restic restore latest --tag mi-diabetes --target $dir
$dump = Get-ChildItem -Recurse $dir -Filter *.dump | Select-Object -First 1
$esperado = Get-Content "$($dump.FullName).recuentos.txt"

docker rm -f md-restauracion 2>$null | Out-Null
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
