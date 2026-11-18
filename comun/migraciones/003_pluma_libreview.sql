-- 003: dosis de la pluma leídas de LibreView (fase 2, tarea 5).
-- Las dosis en sí van a la tabla `dosis` (origen 'pluma_libreview'); aquí solo lo propio del script:
-- cuándo se vio cada dosis por primera vez (para medir el retraso) y su envío a Nightscout.

-- ------------------------------------------------------------------ llegadas
-- Una fila por dosis de la pluma, escrita en la misma transacción que la dosis.
-- `modo` distingue la importación del historial (retraso sin sentido) del ciclo normal.
CREATE TABLE llegadas_pluma (
    dosis_id     bigint PRIMARY KEY REFERENCES dosis (id) ON DELETE CASCADE,
    vista_en     timestamptz NOT NULL DEFAULT now(),
    modo         text NOT NULL CHECK (modo IN ('historico', 'incremental')),
    serie_pluma  text,
    cebado       boolean NOT NULL DEFAULT false,   -- dosis de purgado/aire, si la API lo indica
    bruto        jsonb NOT NULL                    -- registro de LibreView tal cual (solo en la base)
);
CREATE INDEX llegadas_pluma_vista ON llegadas_pluma (vista_en DESC);

-- ------------------------------------------------------------------ envíos a Nightscout
-- Cola de escritura: una fila por dosis que hay que llevar a Nightscout.
CREATE TABLE envios_nightscout (
    dosis_id       bigint PRIMARY KEY REFERENCES dosis (id) ON DELETE CASCADE,
    estado         text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'enviado', 'error')),
    nightscout_id  text,
    intentos       integer NOT NULL DEFAULT 0,
    enviado_en     timestamptz,
    error          text,
    creado_en      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX envios_nightscout_pendientes ON envios_nightscout (creado_en) WHERE estado <> 'enviado';

-- ------------------------------------------------------------------ retraso
-- Retraso real de cada dosis de la pluma: inyección (hora de la pluma) → visto por el script.
-- Si la conciliación la ha emparejado con una dosis confirmada en la app, también el retraso
-- de la app (inyección → confirmación) para compararlos.
CREATE VIEW retraso_pluma AS
SELECT d.id                                   AS dosis_id,
       d.ts                                   AS ts_inyeccion,
       d.unidades,
       l.vista_en,
       l.modo,
       extract(epoch FROM l.vista_en - d.ts) / 60.0           AS retraso_pluma_min,
       app.id                                 AS dosis_app_id,
       app.creado_en                          AS confirmada_app_en,
       extract(epoch FROM app.creado_en - d.ts) / 60.0        AS retraso_app_min
FROM dosis d
JOIN llegadas_pluma l ON l.dosis_id = d.id
LEFT JOIN dosis app ON app.origen = 'app'
                   AND (app.id = d.dosis_par_id OR app.dosis_par_id = d.id)
WHERE d.origen = 'pluma_libreview';
