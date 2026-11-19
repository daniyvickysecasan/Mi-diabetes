# Copia cifrada de la base de datos y de las fotos de las comidas con restic.
# Uso (PowerShell, desde la carpeta despliegue):  .\restic\copia.ps1
# Programar a diario: Programador de tareas → Crear tarea básica → powershell -File ...\copia.ps1
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
$fecha = Get-Date -Format "yyyyMMdd-HHmm"
$tmp = Join-Path $env:TEMP "mi-diabetes-$fecha.dump"
$fotos = Join-Path $env:TEMP "mi-diabetes-fotos-$fecha"
Push-Location (Split-Path -Parent $aqui)
try {
    # Volcado en formato custom (comprimido). Se hace dentro del contenedor y se copia fuera.
    docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/copia.dump'
    docker compose cp db:/tmp/copia.dump $tmp
    docker compose exec -T db rm -f /tmp/copia.dump
    # Recuentos para comprobar la restauración después.
    # El SQL va por stdin: Windows PowerShell 5.1 quita las comillas dobles internas de los argumentos.
    $recuentos = "SELECT 'glucosa', count(*) FROM glucosa UNION ALL SELECT 'dosis', count(*) FROM dosis UNION ALL SELECT 'comidas', count(*) FROM comidas;" |
        docker compose exec -T db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -At -v ON_ERROR_STOP=1'
    if ($LASTEXITCODE -ne 0 -or -not $recuentos) { throw "No se pudieron obtener los recuentos" }
    $recuentos | Set-Content "$tmp.recuentos.txt"
    # Fotos de las comidas (volumen fotos-comidas, montado en la api): se copian fuera y entran en la copia.
    docker compose cp api:/datos/fotos $fotos
    if ($LASTEXITCODE -ne 0) { throw "No se pudieron copiar las fotos de las comidas" }
    # En Windows PowerShell 5.1 el stderr de un programa con "Stop" corta el script: comprobar con "Continue"
    $ErrorActionPreference = "Continue"; restic cat config *> $null; $existe = ($LASTEXITCODE -eq 0); $ErrorActionPreference = "Stop"
    if (-not $existe) { restic init }
    restic backup $tmp "$tmp.recuentos.txt" $fotos --tag mi-diabetes
    # Las rutas llevan la fecha: agrupar por etiqueta para que la retención funcione de verdad.
    restic forget --tag mi-diabetes --group-by host,tags --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --prune
    restic check
} finally {
    Remove-Item -ErrorAction SilentlyContinue $tmp, "$tmp.recuentos.txt"
    Remove-Item -ErrorAction SilentlyContinue -Recurse -Force $fotos
    Pop-Location
}
Write-Host "Copia terminada: $fecha"
