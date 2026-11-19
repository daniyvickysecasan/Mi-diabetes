# Despliegue en el PC (Windows + Docker Desktop + WSL2)

Todo corre en tu PC. Ningún dato sale de casa salvo a través del túnel de Cloudflare.

## 0. Requisitos

- Docker Desktop con el motor WSL2 activado (Settings → General → *Use the WSL 2 based engine*).
- Git para Windows. BitLocker activo en el disco donde vive Docker.
- Opcional para copias: `winget install restic.restic`.

## 1. Descargar y configurar

```powershell
git clone https://github.com/daniyvickysecasan/Mi-diabetes.git
cd Mi-diabetes\despliegue
copy .env.example .env
notepad .env
```

Rellena como mínimo:

| Variable | Qué poner |
|---|---|
| `POSTGRES_PASSWORD` | Una contraseña larga y nueva. |
| `NIGHTSCOUT_URL` | URL de tu Nightscout **vista desde Docker** (ver abajo). |
| `NIGHTSCOUT_TOKEN` | Token de un sujeto con rol **readable** (solo lectura). |
| `CODIGO_ALTA` | Un código largo para dar de alta tu passkey. Bórralo después. |
| `CLOUDFLARE_TUNNEL_TOKEN` | Cuando configures el túnel (paso 5). |

**URL de Nightscout.** Si Nightscout corre en el mismo Docker Desktop, únelo a la red
`mi-diabetes_default` (`docker network connect mi-diabetes_default <contenedor-nightscout>`) y usa
`http://<contenedor-nightscout>:1337`. Si corre fuera de Docker en el PC, usa
`http://host.docker.internal:1337`. Comprueba el puerto real de tu instalación.

**Token de solo lectura.** Nightscout → menú → *Admin Tools* → *Subjects* → *Add new Subject*,
nombre `mi-diabetes`, rol `readable`. Copia el token (`mi-diabetes-xxxxxxxxxxxx`). No uses el
`API_SECRET`.

## 2. Arrancar

```powershell
docker compose up -d --build
docker compose ps                    # db healthy, migraciones Exited (0), api y recolector Up
curl http://localhost:8000/salud     # {"ok":true}
```

## 3. Importar todo el histórico y comprobarlo

```powershell
docker compose run --rm recolector python -m servicios.recoleccion historico --desde 2020-01-01
docker compose run --rm recolector python -m servicios.recoleccion verificar --desde 2020-01-01
```

`verificar` termina con `"ok": true` si la base tiene exactamente las mismas lecturas y valores
que Nightscout. Puedes repetir `historico` cuando quieras: no duplica nada. Si se corta, vuelve
a lanzarlo y sigue.

Desde ese momento el contenedor `recolector` lee cada 5 minutos (con 3 h de solape por si
LibreLinkUp sube lecturas tarde) y marca los huecos de más de 20 min. Ver los registros:
`docker compose logs -f recolector`.

## 4. Primera passkey

La passkey queda atada al dominio (`RP_ID`). Para usarla en el iPhone, el alta se hace ya en
`https://midiabetes.intelligentcontrol.net` (paso 5). Para probar antes en el PC, pon temporalmente
`RP_ID=localhost`, `ORIGENES=http://localhost:8000`, `COOKIE_SEGURA=0`, reinicia
(`docker compose up -d`) y abre http://localhost:8000.

En la app: *Dar de alta una passkey* → escribe `CODIGO_ALTA` → Face ID. Después borra
`CODIGO_ALTA` del `.env` y reinicia `api`. Para añadir otra passkey más tarde hace falta el código
**y** una sesión abierta.

## 5. Túnel y Access

Sigue `cloudflared/README.md` y arranca con `docker compose --profile tunel up -d`.
En el iPhone abre `https://midiabetes.intelligentcontrol.net` en Safari → Compartir → *Añadir a pantalla de inicio*.

## 6. Copias cifradas (restic)

```powershell
copy restic\restic.env.example restic\restic.env
notepad restic\restic.env
.\restic\copia.ps1                 # primera copia (crea el repositorio)
.\restic\prueba_restauracion.ps1   # restaura en una base vacía y compara recuentos
```

Programa `copia.ps1` a diario (PowerShell, desde `despliegue`):

```powershell
$accion = New-ScheduledTaskAction -Execute powershell.exe -WorkingDirectory "$PWD" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\restic\copia.ps1`""
Register-ScheduledTask -TaskName "Mi Diabetes - copia" -Action $accion -Trigger (New-ScheduledTaskTrigger -Daily -At 03:30)
```

Solo se ejecuta con el PC encendido, la sesión iniciada y Docker Desktop abierto.
Guarda la contraseña de restic fuera del PC: sin ella no se puede restaurar.

## Fase 2: importar el CSV de LibreView

El CSV es un dato de salud: guárdalo fuera del repositorio (por ejemplo en `D:\Proyecto personal\MiDiabetes\datos\`)
y no lo subas a ningún sitio. Se monta en el contenedor solo para leerlo.

1. En LibreView (web): **Informes** → **Descargar datos de glucosa** (icono de descarga, arriba a la derecha) → guarda el CSV.
2. Simula primero (no escribe nada; enseña columnas, fechas y recuentos):

```powershell
cd "D:\Proyecto personal\MiDiabetes\Mi-diabetes\despliegue"
docker compose run --rm -v "D:\Proyecto personal\MiDiabetes\datos:/datos:ro" api `
  python -m servicios.recoleccion.csv_libreview /datos/NOMBRE_DEL_ARCHIVO.csv --simular
```

3. Si las fechas y las unidades cuadran, importa (es idempotente: repetirlo no duplica):

```powershell
docker compose run --rm -v "D:\Proyecto personal\MiDiabetes\datos:/datos:ro" api `
  python -m servicios.recoleccion.csv_libreview /datos/NOMBRE_DEL_ARCHIVO.csv
```

El resumen final dice cuántas dosis quedan emparejadas con la app, solo en la pluma, duplicadas
(descartadas) o por revisar. `sin_marcar` debe ser 0. Repite el paso 3 cada semana como respaldo.

### Comparar las métricas con Nightscout o LibreView

```powershell
docker compose run --rm api python -m comun.metricas --desde 2026-11-04 --hasta 2026-11-17
```

Compara TIR, GMI y CV con el informe del mismo periodo (criterio: TIR a menos de 1 punto, GMI a menos de 0,1).

## Fase 2: dosis de la pluma leídas solas de LibreView

El recolector `recolector-pluma` pide cada 10 minutos a la web de LibreView el informe "Registro diario",
que trae la hora y las unidades de cada inyección de la pluma (sin los cebados marcados en LibreLink).
Es una API no oficial: si LibreView la cambia, el panel lo avisa y siguen valiendo la app y el CSV.

1. En `despliegue\.env` rellena `LIBREVIEW_EMAIL` y `LIBREVIEW_PASSWORD` (los de libreview.com).
   Deja `NIGHTSCOUT_TOKEN_ESCRITURA` vacío la primera vez: así las dosis solo van a la base.
2. Arráncalo:
   ```powershell
   docker compose build
   docker compose --profile pluma up -d
   ```
3. Abre la app en el iPhone. Saldrá el aviso "LibreView pide el código del SMS": pulsa
   **Enviarme el SMS** y escribe el código que te llegue. Desde ahí va solo; LibreView recuerda el
   dispositivo (unas dos semanas) y, cuando vuelva a pedirlo, la app te lo dirá igual.
4. Comprueba: `docker compose logs --tail 20 recolector-pluma` debe decir "informe: N dosis nuevas".
5. Retraso medido: `docker compose run --rm recolector-pluma python -m servicios.recoleccion.pluma_cli retraso`.

Si en una misma hora te pinchas dos veces, LibreView solo da la suma de esa hora: esas dosis no se
reparten a ojo y se quedan esperando a la app o al CSV.

## Probar sin datos reales

```powershell
# en .env: NIGHTSCOUT_URL=http://nightscout-simulado:1337 y NIGHTSCOUT_TOKEN=prueba-lectura
docker compose --profile simulado up -d --build
```

El simulador genera 30 días sintéticos y añade una lectura cada 5 min. Usa otra base
(`docker compose down -v` borra el volumen) antes de pasar a tus datos reales.

## Actualizar

```powershell
git pull
docker compose up -d --build       # las migraciones nuevas se aplican solas al arrancar
```
