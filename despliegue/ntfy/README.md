# ntfy: avisos de la fase 4 en el iPhone

El servicio `anomalias` publica en el ntfy de casa (contenedor `ntfy`); la app ntfy del iPhone lo
recibe por `https://ntfy.intelligentcontrol.net`. Las respuestas se dan en Mi Diabetes (pantalla
Avisos, detrás de Access y la passkey), no en ntfy.

## Una vez, en el PC (PowerShell, en `despliegue`)

1. Elige un tema largo y aleatorio y ponlo en `.env` como `NTFY_TEMA` (es un secreto):
   `-join ((48..57)+(97..122) | Get-Random -Count 24 | % {[char]$_})`
2. Arranca: `docker compose up -d ntfy`
3. Usuario del iPhone (solo lectura) y usuario del servicio (solo escritura):
   ```
   docker compose exec ntfy ntfy user add daniel
   docker compose exec ntfy ntfy user add midiabetes
   docker compose exec ntfy ntfy access daniel <TEMA> read-only
   docker compose exec ntfy ntfy access midiabetes <TEMA> write-only
   docker compose exec ntfy ntfy token add midiabetes
   ```
   Copia el token (`tk_…`) a `.env` como `NTFY_TOKEN`. En `.env` también: `NTFY_URL=http://ntfy:80`
   y `URL_APP=https://midiabetes.intelligentcontrol.net`.
4. Cloudflare Zero Trust: ruta pública `ntfy.intelligentcontrol.net` → `http://ntfy:80` y aplicación
   de Access con política *Bypass* (ver `../cloudflared/README.md`). La protege `deny-all` + usuario.
5. `docker compose --profile tunel up -d --build` (levanta también `anomalias`).
6. iPhone: instala **ntfy** (App Store, gratis) → Ajustes → *Default server* (o al suscribirte,
   *Use another server*) `https://ntfy.intelligentcontrol.net` → suscríbete a `<TEMA>` → inicia
   sesión con el usuario `daniel`. Permite las notificaciones; para la acumulación de insulina
   conviene permitir también las *críticas/urgentes*.
7. Prueba: `docker compose exec anomalias python -m servicios.anomalias probar-aviso`

## Privacidad

Con `upstream-base-url` (en `server.yml`) ntfy.sh recibe solo un identificador del mensaje, nunca
el texto. Si prefieres que nada salga de casa, borra esa línea y reinicia ntfy: los avisos llegarán
cuando abras la app ntfy, no al momento.
