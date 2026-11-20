-- 010: anomalías y avisos por ntfy (fase 4).
-- El servicio de anomalías (servicios/anomalias) detecta episodios con reglas deterministas,
-- los guarda aquí y los publica en el ntfy de casa. Daniel responde en la app y su respuesta queda
-- como etiqueta. NADA de esta tabla entra en un cálculo de dosis (hay un test que lo comprueba).

CREATE TABLE anomalias (
    id                bigserial PRIMARY KEY,
    tipo              text NOT NULL CHECK (tipo IN
                        ('hueco', 'presion', 'subida_sin_comida', 'dosis_sin_comida', 'acumulacion', 'olvido_tresiba')),
    inicio            timestamptz NOT NULL,
    fin               timestamptz,                 -- NULL = sigue abierto (p. ej. un hueco)
    urgente           boolean NOT NULL DEFAULT false,
    titulo            text NOT NULL,
    texto             text NOT NULL,
    pregunta          text NOT NULL,
    opciones          jsonb NOT NULL,              -- [{"clave", "texto", "falsa_alarma"}]
    datos             jsonb NOT NULL DEFAULT '{}', -- valores medidos que la dispararon
    detector_version  text NOT NULL,
    umbrales          jsonb NOT NULL,              -- umbrales con los que se detectó
    dosis_id          bigint REFERENCES dosis (id),
    estado            text NOT NULL DEFAULT 'abierta' CHECK (estado IN ('abierta', 'respondida', 'descartada')),
    respuesta         text,                        -- clave de la opción elegida: la etiqueta
    nota              text,
    respondida_en     timestamptz,
    -- Notificación
    aviso_estado      text NOT NULL DEFAULT 'pendiente' CHECK (aviso_estado IN
                        ('pendiente', 'enviado', 'retenido', 'error', 'no_enviar')),
    avisar_desde      timestamptz NOT NULL,        -- las no urgentes esperan al final del silencio nocturno
    avisado_en        timestamptz,
    aviso_intentos    integer NOT NULL DEFAULT 0,
    aviso_error       text,
    detectada_en      timestamptz NOT NULL DEFAULT now(),
    actualizada_en    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fin_tras_inicio CHECK (fin IS NULL OR fin >= inicio),
    CONSTRAINT respondida_con_respuesta CHECK
        (estado <> 'respondida' OR (respuesta IS NOT NULL AND respondida_en IS NOT NULL)),
    CONSTRAINT abierta_sin_respuesta CHECK (estado <> 'abierta' OR respuesta IS NULL),
    CONSTRAINT enviada_con_fecha CHECK (aviso_estado <> 'enviado' OR avisado_en IS NOT NULL)
);
CREATE INDEX anomalias_tipo_inicio ON anomalias (tipo, inicio DESC);
CREATE INDEX anomalias_abiertas ON anomalias (detectada_en DESC) WHERE estado = 'abierta';
CREATE INDEX anomalias_por_avisar ON anomalias (avisar_desde) WHERE aviso_estado = 'pendiente';
-- Una dosis solo da una pregunta de "Fiasp sin comida".
CREATE UNIQUE INDEX anomalias_dosis_unica ON anomalias (dosis_id) WHERE dosis_id IS NOT NULL;

-- Episodios que Daniel etiqueta a mano en su histórico, para medir el criterio de la fase 4
-- (detectar ≥ 80 % con ≤ 1 falsa alarma diaria de media): python -m servicios.anomalias evaluar
CREATE TABLE episodios_etiquetados (
    id         bigserial PRIMARY KEY,
    tipo       text NOT NULL CHECK (tipo IN
                 ('hueco', 'presion', 'subida_sin_comida', 'dosis_sin_comida', 'acumulacion', 'olvido_tresiba')),
    inicio     timestamptz NOT NULL,
    fin        timestamptz NOT NULL,
    notas      text,
    creado_en  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fin_tras_inicio CHECK (fin >= inicio)
);
CREATE INDEX episodios_etiquetados_inicio ON episodios_etiquetados (inicio);

-- Estado del servicio (fila única), para verlo en la app.
CREATE TABLE estado_anomalias (
    unica          boolean PRIMARY KEY DEFAULT true CHECK (unica),
    ultimo_ciclo   timestamptz,
    ultimo_error   text,
    ntfy_ok        timestamptz,
    ntfy_error     text
);
INSERT INTO estado_anomalias DEFAULT VALUES;
