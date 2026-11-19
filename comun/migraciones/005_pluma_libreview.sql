-- 005: dosis de la pluma leídas de LibreView (fase 2, tarea 5).
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

-- ------------------------------------------------------------------ horas con varias inyecciones
-- El informe "Registro diario" de LibreView da la hora de cada inyección pero solo la suma de
-- unidades por hora. Si en una hora hay más de una inyección no se puede saber cuánto fue cada
-- una: esas horas NO entran en `dosis` (las cubren la app y el CSV); se guardan aquí para contrastar.
CREATE TABLE horas_pluma_libreview (
    hora            timestamptz PRIMARY KEY,        -- inicio de la hora local, en UTC
    inyecciones     timestamptz[] NOT NULL,
    unidades_total  numeric(6,2) NOT NULL,
    serie_pluma     text,
    vista_en        timestamptz NOT NULL DEFAULT now(),
    actualizada_en  timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ sesión con LibreView
-- Una sola fila. El recolector y la API solo se comunican por aquí (regla 3).
-- Flujo del SMS: el recolector no puede entrar → 'necesita_codigo'; desde la app se pide el SMS →
-- 'sms_pedido'; el recolector lo pide a LibreView → 'esperando_codigo'; se escribe el código en la
-- app → 'codigo_recibido'; el recolector lo envía, borra el código y vuelve a 'ok'.
-- El código es de un solo uso y se borra en cuanto se usa o caduca.
CREATE TABLE estado_libreview (
    id                    boolean PRIMARY KEY DEFAULT true CHECK (id),
    estado                text NOT NULL DEFAULT 'sin_iniciar' CHECK (estado IN
                            ('sin_iniciar', 'ok', 'necesita_codigo', 'sms_pedido', 'esperando_codigo',
                             'codigo_recibido', 'error')),
    codigo                text CHECK (codigo IS NULL OR codigo ~ '^[0-9]{4,8}$'),
    destino_sms           text,                      -- teléfono enmascarado, tal como lo da LibreView
    cambiado_en           timestamptz NOT NULL DEFAULT now(),
    ultimo_ok             timestamptz,               -- última lectura del informe sin errores
    ultima_sincronizacion timestamptz,               -- última vez que la pluma subió datos a LibreView
    error                 text
);
INSERT INTO estado_libreview DEFAULT VALUES;

-- Lectura bajo demanda: la API (botón de la app o, en la fase 3, el cálculo de una dosis) inserta una
-- fila; el recolector la ve en segundos, lee LibreView, concilia y la cierra.
CREATE TABLE solicitudes_lectura_pluma (
    id          bigserial PRIMARY KEY,
    pedida_en   timestamptz NOT NULL DEFAULT now(),
    atendida_en timestamptz,
    resultado   text CHECK (resultado IN ('ok', 'error', 'sin_sesion')),
    detalle     text,
    CHECK ((atendida_en IS NULL) = (resultado IS NULL))
);
CREATE INDEX solicitudes_lectura_pluma_abiertas ON solicitudes_lectura_pluma (pedida_en) WHERE atendida_en IS NULL;

-- Dosis ya guardadas desde el informe que una lectura posterior contradice (cambia el total de su
-- hora, aparece otra inyección en esa hora o desaparece: p. ej. un cebado marcado después).
-- La dosis NO se toca: solo se anota para revisarla.
CREATE TABLE cambios_pluma_libreview (
    dosis_id   bigint NOT NULL REFERENCES dosis (id) ON DELETE CASCADE,
    motivo     text NOT NULL CHECK (motivo IN ('total_distinto', 'otra_inyeccion', 'desaparecida')),
    detalle    jsonb NOT NULL DEFAULT '{}',
    visto_en   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dosis_id, motivo)
);
