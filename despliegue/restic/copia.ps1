# Copia cifrada de la base de datos con restic.
# Uso (PowerShell, desde la carpeta despliegue):  .\restic\copia.ps1
# Programar a diario: Programador de tareas → Crear tarea básica → powershell -File ...\copia.ps1
$ErrorActionPreference = "Stop"
$aqui = Split-Path -Parent $MyInvocation.MyCommand.Path
Get-Content "$aqui\restic.env" | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}
$fecha = Get-Date -Format "yyyyMMdd-HHmm"
$tmp = Join-Path $env:TEMP "mi-diabetes-$fecha.dump"
Push-Location (Split-Path -Parent $aqui)
try {
    # Volcado en formato custom (comprimido). Se hace dentro del contenedor y se copia fuera.
    docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/copia.dump'
    docker compose cp db:/tmp/copia.dump $tmp
    docker compose exec -T db rm -f /tmp/copia.dump
    # Recuentos para comprobar la restauración después
    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT ''glucosa'', count(*) FROM glucosa UNION ALL SELECT ''dosis'', count(*) FROM dosis UNION ALL SELECT ''comidas'', count(*) FROM comidas"' |
        Set-Content "$tmp.recuentos.txt"
    if (-not (restic cat config 2>$null)) { restic init }
    restic backup $tmp "$tmp.recuentos.txt" --tag mi-diabetes
    restic forget --tag mi-diabetes --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune
    restic check
} finally {
    Remove-Item -ErrorAction SilentlyContinue $tmp, "$tmp.recuentos.txt"
    Pop-Location
}
Write-Host "Copia terminada: $fecha"
