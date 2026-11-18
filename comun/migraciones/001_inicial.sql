-- 001: esquema inicial de Mi Diabetes (fase 1).
-- Todas las horas son timestamptz (UTC). Las unidades de glucosa son mg/dL.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ------------------------------------------------------------------ glucosa
-- Una fila por lectura. La clave (ts, fuente, tipo) hace idempotente la importación:
-- reimportar el mismo periodo no duplica nada.
CREATE TABLE glucosa (
    ts           timestamptz NOT NULL,
    fuente       text        NOT NULL DEFAULT 'nightscout',
    tipo         text        NOT NULL DEFAULT 'sgv' CHECK (tipo IN ('sgv', 'mbg')),
    valor_mgdl   smallint    NOT NULL CHECK (valor_mgdl BETWEEN 20 AND 600),
    tendencia    text,                       -- DoubleUp, SingleUp, ..., Flat, ..., DoubleDown
    delta_mgdl   real,
    dispositivo  text,
    id_externo   text,                       -- _id de Nightscout
    recibido_en  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ts, fuente, tipo)
);
SELECT create_hypertable('glucosa', 'ts', chunk_time_interval => interval '30 days');
CREATE INDEX glucosa_fuente_ts ON glucosa (fuente, ts DESC);

-- ------------------------------------------------------------------ comidas
CREATE TABLE comidas (
    id               bigserial PRIMARY KEY,
    ts               timestamptz NOT NULL,
    origen           text NOT NULL CHECK (origen IN ('foto', 'texto', 'chat', 'manual')),
    descripcion      text,
    foto_ruta        text,                   -- ruta local fuera del repositorio
    hidratos_g       numeric(6,1),
    fibra_g          numeric(6,1),
    azucares_g       numeric(6,1),
    grasa_g          numeric(6,1),
    proteina_g       numeric(6,1),
    kcal             numeric(7,1),
    hidratos_bolo_g  numeric(6,1),           -- hidratos que cuentan para el bolo
    rango_hidratos_min_g numeric(6,1),
    rango_hidratos_max_g numeric(6,1),
    items            jsonb,                  -- tabla revisada por el usuario
    estimaciones     jsonb,                  -- estimaciones por modelo y vía
    alertas          jsonb,
    revisada         boolean NOT NULL DEFAULT false,
    creado_en        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX comidas_ts ON comidas (ts DESC);

-- ------------------------------------------------------------------ dosis
-- Cada dosis guarda su origen y su estado de conciliación.
-- Regla: la pluma manda en hora y unidades; la app manda en comida e insulina.
CREATE TABLE dosis (
    id                  bigserial PRIMARY KEY,
    ts                  timestamptz NOT NULL,
    unidades            numeric(5,2) NOT NULL CHECK (unidades > 0),
    insulina            text NOT NULL CHECK (insulina IN ('fiasp', 'tresiba')),
    origen              text NOT NULL CHECK (origen IN
                          ('app', 'pluma_libreview', 'csv_libreview', 'nightscout', 'tresiba_programada', 'manual')),
    id_externo          text,                -- id en LibreView, CSV o Nightscout
    estado_conciliacion text NOT NULL DEFAULT 'pendiente' CHECK (estado_conciliacion IN
                          ('pendiente', 'emparejada', 'discrepancia', 'solo_app', 'solo_pluma', 'descartada')),
    dosis_par_id        bigint REFERENCES dosis (id),   -- la otra mitad de una pareja app/pluma
    comida_id           bigint REFERENCES comidas (id),
    sugerencia_id       bigint,              -- FK añadida tras crear sugerencias
    notas               text,
    creado_en           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (origen, id_externo)
);
CREATE INDEX dosis_ts ON dosis (ts DESC);
CREATE INDEX dosis_pendientes ON dosis (estado_conciliacion) WHERE estado_conciliacion IN ('pendiente', 'discrepancia');

-- ------------------------------------------------------------------ hidratos de corrección
-- Hidratos sin dosis (hipos, picoteo): botón rápido con gramos.
CREATE TABLE hidratos_correccion (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL,
    gramos     numeric(5,1) NOT NULL CHECK (gramos > 0),
    motivo     text NOT NULL DEFAULT 'hipoglucemia' CHECK (motivo IN ('hipoglucemia', 'picoteo', 'ejercicio', 'otro')),
    notas      text,
    creado_en  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX hidratos_correccion_ts ON hidratos_correccion (ts DESC);

-- ------------------------------------------------------------------ Tresiba
-- La dosis diaria se da por puesta. Cada cambio de dosis u hora es una fila nueva.
CREATE TABLE config_tresiba (
    id             bigserial PRIMARY KEY,
    vigente_desde  timestamptz NOT NULL UNIQUE,
    unidades       numeric(5,2) NOT NULL CHECK (unidades > 0),
    hora_local     time NOT NULL,
    zona_horaria   text NOT NULL DEFAULT 'Europe/Madrid',
    notas          text,
    creado_en      timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ eventos de chat
-- Texto libre convertido en evento estructurado e incompleto. Nunca produce dosis.
CREATE TABLE eventos_chat (
    id                  bigserial PRIMARY KEY,
    ts_mensaje          timestamptz NOT NULL DEFAULT now(),
    texto               text NOT NULL,
    tipo                text CHECK (tipo IN ('comida', 'bebida', 'alcohol', 'ejercicio', 'enfermedad', 'estres', 'sueno', 'otro')),
    ts_evento_inicio    timestamptz,
    ts_evento_fin       timestamptz,
    estimacion          jsonb,               -- p. ej. {"hidratos_g": [20, 45]}
    incertidumbre       text CHECK (incertidumbre IN ('baja', 'media', 'alta')),
    etiquetas           text[] NOT NULL DEFAULT '{}',
    modelo              text,                -- modelo local que lo interpretó
    estado              text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'confirmado', 'descartado')),
    comida_id           bigint REFERENCES comidas (id),
    creado_en           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eventos_chat_ts ON eventos_chat (ts_mensaje DESC);

-- ------------------------------------------------------------------ versiones de parámetros
-- Ratio y sensibilidad por franja, objetivo. Ningún cambio se activa sin aprobación.
CREATE TABLE versiones_parametros (
    id                       bigserial PRIMARY KEY,
    version                  integer NOT NULL UNIQUE,
    estado                   text NOT NULL DEFAULT 'propuesta' CHECK (estado IN
                               ('propuesta', 'sombra', 'activa', 'rechazada', 'retirada')),
    origen                   text NOT NULL CHECK (origen IN ('manual', 'autotune', 'importado')),
    ratio_hc                 jsonb NOT NULL,   -- [{"desde": "07:00", "g_por_u": 10}, ...]
    sensibilidad             jsonb NOT NULL,   -- [{"desde": "00:00", "mgdl_por_u": 40}, ...]
    objetivo_mgdl            smallint NOT NULL DEFAULT 90,
    objetivo_correccion_mgdl smallint NOT NULL DEFAULT 110,
    confianza                text NOT NULL DEFAULT 'baja' CHECK (confianza IN ('baja', 'media', 'alta')),
    motivo                   text,
    metricas                 jsonb,
    aprobada_por             text,
    aprobada_en              timestamptz,
    vigente_desde            timestamptz,
    creado_en                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT activa_requiere_aprobacion CHECK
        (estado NOT IN ('activa', 'sombra') OR (aprobada_en IS NOT NULL AND aprobada_por IS NOT NULL))
);
CREATE UNIQUE INDEX una_version_activa ON versiones_parametros ((true)) WHERE estado = 'activa';

-- ------------------------------------------------------------------ sugerencias
-- Toda cifra procede de un cálculo determinista versionado; se guarda la traza completa.
CREATE TABLE sugerencias (
    id                     bigserial PRIMARY KEY,
    ts                     timestamptz NOT NULL DEFAULT now(),
    comida_id              bigint REFERENCES comidas (id),
    version_parametros_id  bigint NOT NULL REFERENCES versiones_parametros (id),
    calculo_version        text NOT NULL,      -- versión del código determinista que la produjo
    glucosa_mgdl           smallint,
    tendencia              text,
    insulina_activa_u      numeric(5,2),
    hidratos_g             numeric(6,1),
    u_hidratos             numeric(5,2),
    u_correccion           numeric(5,2),
    u_inmediata            numeric(5,2),
    u_segunda              numeric(5,2),       -- grasa y proteína: segunda inyección
    ts_segunda             timestamptz,
    incertidumbre          jsonb NOT NULL,     -- rango y motivos
    bloqueada              boolean NOT NULL DEFAULT false,
    motivo_bloqueo         text,
    avisos                 jsonb NOT NULL DEFAULT '[]',
    traza                  jsonb NOT NULL,     -- entradas y pasos del cálculo
    CONSTRAINT bloqueada_sin_dosis CHECK (NOT bloqueada OR (u_inmediata IS NULL AND u_segunda IS NULL)),
    CONSTRAINT bloqueo_con_motivo CHECK (NOT bloqueada OR motivo_bloqueo IS NOT NULL)
);
CREATE INDEX sugerencias_ts ON sugerencias (ts DESC);

ALTER TABLE dosis ADD CONSTRAINT dosis_sugerencia_fk FOREIGN KEY (sugerencia_id) REFERENCES sugerencias (id);

-- ------------------------------------------------------------------ decisiones
CREATE TABLE decisiones (
    id                bigserial PRIMARY KEY,
    sugerencia_id     bigint NOT NULL REFERENCES sugerencias (id),
    ts                timestamptz NOT NULL DEFAULT now(),
    decision          text NOT NULL CHECK (decision IN ('hecho', 'modificado', 'ignorado')),
    unidades_reales   numeric(5,2),
    dosis_id          bigint REFERENCES dosis (id),
    notas             text,
    resultado         jsonb,                   -- glucosa a 3–4 h, TIR posprandial…
    evaluado_en       timestamptz
);
CREATE INDEX decisiones_sugerencia ON decisiones (sugerencia_id);

-- ------------------------------------------------------------------ tratamientos de Nightscout
-- Copia en bruto de los tratamientos leídos de Nightscout (sin los de nuestro origen propio).
-- No alimentan las dosis directamente: la conciliación de la fase 2 decide qué hacer con ellos.
CREATE TABLE tratamientos_nightscout (
    id_externo   text PRIMARY KEY,
    ts           timestamptz NOT NULL,
    tipo_evento  text,
    insulina_u   numeric(5,2),
    hidratos_g   numeric(6,1),
    introducido_por text,
    datos        jsonb NOT NULL,
    recibido_en  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tratamientos_nightscout_ts ON tratamientos_nightscout (ts DESC);

-- ------------------------------------------------------------------ recolección
CREATE TABLE ejecuciones_recoleccion (
    id           bigserial PRIMARY KEY,
    fuente       text NOT NULL,
    modo         text NOT NULL CHECK (modo IN ('historico', 'incremental', 'verificacion')),
    inicio       timestamptz NOT NULL DEFAULT now(),
    fin          timestamptz,
    desde        timestamptz,
    hasta        timestamptz,
    leidas       integer NOT NULL DEFAULT 0,
    insertadas   integer NOT NULL DEFAULT 0,
    ignoradas    integer NOT NULL DEFAULT 0,
    conflictos   integer NOT NULL DEFAULT 0,  -- misma hora, distinto valor
    error        text
);
CREATE INDEX ejecuciones_fuente_inicio ON ejecuciones_recoleccion (fuente, inicio DESC);

CREATE TABLE huecos (
    id            bigserial PRIMARY KEY,
    fuente        text NOT NULL,
    inicio        timestamptz NOT NULL,       -- última lectura antes del hueco
    fin           timestamptz,                -- primera lectura después (NULL = sigue abierto)
    minutos       integer,
    estado        text NOT NULL DEFAULT 'abierto' CHECK (estado IN ('abierto', 'cerrado', 'rellenado')),
    detectado_en  timestamptz NOT NULL DEFAULT now(),
    actualizado_en timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fuente, inicio)
);

-- ------------------------------------------------------------------ autenticación (passkey)
CREATE TABLE credenciales_passkey (
    id_credencial  bytea PRIMARY KEY,
    clave_publica  bytea NOT NULL,
    contador       bigint NOT NULL DEFAULT 0,
    transportes    text[] NOT NULL DEFAULT '{}',
    nombre         text NOT NULL DEFAULT 'passkey',
    creado_en      timestamptz NOT NULL DEFAULT now(),
    ultimo_uso     timestamptz
);

CREATE TABLE retos_webauthn (
    id         text PRIMARY KEY,
    reto       bytea NOT NULL,
    tipo       text NOT NULL CHECK (tipo IN ('alta', 'entrada')),
    expira_en  timestamptz NOT NULL
);

CREATE TABLE sesiones (
    hash_token     bytea PRIMARY KEY,         -- sha256 del token de la cookie
    id_credencial  bytea REFERENCES credenciales_passkey (id_credencial) ON DELETE CASCADE,
    creado_en      timestamptz NOT NULL DEFAULT now(),
    expira_en      timestamptz NOT NULL,
    ultimo_uso     timestamptz NOT NULL DEFAULT now()
);
