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

Programa `copia.ps1` a diario en el Programador de tareas. Guarda la contraseña de restic fuera del PC.

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
