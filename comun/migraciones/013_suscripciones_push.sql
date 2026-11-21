-- 013: avisos Web Push a la PWA (fase 4, decisión de Daniel del 21-11-2026).
-- Al pulsar una notificación de ntfy, iOS abre el navegador; con Web Push se abre la PWA de la
-- pantalla de inicio. Cada iPhone/navegador que activa los avisos deja aquí su suscripción.
-- ntfy queda solo para la alerta urgente (acumulación de insulina), como respaldo.

CREATE TABLE suscripciones_push (
    id            bigserial PRIMARY KEY,
    endpoint      text NOT NULL UNIQUE,      -- URL del servicio de push del navegador (Apple, etc.)
    p256dh        text NOT NULL,             -- clave pública del navegador (cifra el aviso)
    auth          text NOT NULL,
    nombre        text,                      -- p. ej. "iPhone"
    activa        boolean NOT NULL DEFAULT true,
    creado_en     timestamptz NOT NULL DEFAULT now(),
    ultimo_ok     timestamptz,
    ultimo_error  text,
    errores       integer NOT NULL DEFAULT 0
);

ALTER TABLE anomalias ADD COLUMN canales jsonb NOT NULL DEFAULT '{}';   -- {"webpush": n, "ntfy": true}
