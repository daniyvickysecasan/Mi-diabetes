-- 009: registro tipo chat (fase 3, tarea 4).
-- La API guarda el texto 'pendiente' de análisis; el trabajador de comidas lo interpreta con un
-- modelo local y deja tipo, horas, estimación con incertidumbre y etiquetas. Daniel lo confirma,
-- lo corrige o lo descarta. Nada de esto entra en un cálculo de dosis (regla 1; hay un test que
-- comprueba que la sugerencia no lee esta tabla). Sin aviso por alcohol (Daniel, 19-11-2026).

ALTER TABLE eventos_chat
    ADD COLUMN estado_analisis text NOT NULL DEFAULT 'pendiente'
        CHECK (estado_analisis IN ('pendiente', 'analizando', 'lista', 'error')),
    ADD COLUMN error_analisis  text,
    ADD COLUMN analizado_en    timestamptz,
    ADD COLUMN resumen         text,                 -- frase corta del modelo para la lista
    ADD COLUMN decidido_en     timestamptz,
    ADD COLUMN corregido       boolean NOT NULL DEFAULT false,
    ADD COLUMN id_cliente      text UNIQUE,
    ADD CONSTRAINT decidido_con_fecha CHECK (estado = 'pendiente' OR decidido_en IS NOT NULL),
    ADD CONSTRAINT horas_coherentes CHECK (
        ts_evento_inicio IS NULL OR ts_evento_fin IS NULL OR ts_evento_inicio <= ts_evento_fin);

CREATE INDEX eventos_chat_por_analizar ON eventos_chat (creado_en) WHERE estado_analisis = 'pendiente';
