# Cloudflare Tunnel + Access (plan gratuito)

Se usa un túnel **gestionado desde el panel** (token), así no hay credenciales en archivos.

1. Zero Trust → Networks → Tunnels → *Create a tunnel* → Cloudflared → nombre `mi-diabetes`.
2. Copia el token del comando de instalación y pégalo en `despliegue/.env` como `CLOUDFLARE_TUNNEL_TOKEN`.
3. En *Public Hostnames* del túnel:
   | Subdominio | Servicio |
   |---|---|
   | `app.intelligentcontrol.net` | `http://api:8000` |
   | `ntfy.intelligentcontrol.net` | `http://ntfy:80` (fase 4) |
4. Zero Trust → Access → Applications → *Self-hosted*:
   - `app.intelligentcontrol.net`: política *Allow* solo para tu correo (One-time PIN o tu proveedor).
     Duración de sesión: 1 mes (la passkey es la segunda barrera).
   - `ntfy.intelligentcontrol.net` (fase 4): política *Bypass* para todos; la protege el token de ntfy.
5. Arranca: `docker compose --profile tunel up -d`.

Comprobación (criterio de la fase 1): desde los datos móviles, sin pasar Access no se ve nada
(ni siquiera `/salud`); con Access + passkey se ve el panel.

`config.yml.ejemplo` es la alternativa con túnel local por archivo, por si algún día lo prefieres.
