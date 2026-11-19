-- 006: sugerencias de dosis de Fiasp (fase 3, tarea 5).
-- Toda cifra sale de comun/sugerencia.py (determinista, con tests). Ningún modelo de lenguaje
-- escribe aquí. Cada fila guarda en `traza` todas las entradas para poder rehacer el cálculo.

ALTER TABLE sugerencias
    -- Una sugerencia bloqueada por falta de parámetros también se guarda (sin versión).
    ALTER COLUMN version_parametros_id DROP NOT NULL,
    ADD COLUMN tipo                 text NOT NULL DEFAULT 'comida'
                                    CHECK (tipo IN ('comida', 'correccion', 'segunda')),
    -- La segunda inyección (grasa y proteína) se vuelve a calcular a su hora: apunta a la original.
    ADD COLUMN sugerencia_origen_id bigint REFERENCES sugerencias (id),
    -- Lectura de la pluma en LibreView lanzada antes del cálculo, y cómo acabó.
    ADD COLUMN solicitud_lectura_id bigint REFERENCES solicitudes_lectura_pluma (id),
    ADD COLUMN estado_pluma         text CHECK (estado_pluma IN ('leida', 'no_leida', 'confirmada_a_mano')),
    ADD COLUMN ts_comida            timestamptz,
    ADD COLUMN grasa_g              numeric(6,1),
    ADD COLUMN proteina_g           numeric(6,1),
    ADD COLUMN ugp                  numeric(5,2),
    ADD CONSTRAINT con_dosis_requiere_parametros CHECK (bloqueada OR version_parametros_id IS NOT NULL),
    ADD CONSTRAINT segunda_con_origen CHECK ((tipo = 'segunda') = (sugerencia_origen_id IS NOT NULL)),
    ADD CONSTRAINT unidades_no_negativas CHECK
        (coalesce(u_inmediata, 0) >= 0 AND coalesce(u_segunda, 0) >= 0),
    ADD CONSTRAINT segunda_con_hora CHECK ((u_segunda IS NULL) = (ts_segunda IS NULL));

CREATE INDEX sugerencias_origen ON sugerencias (sugerencia_origen_id) WHERE sugerencia_origen_id IS NOT NULL;

-- Una sugerencia no se corrige nunca: lo que Daniel haga con ella va a `decisiones`.
CREATE FUNCTION sugerencias_inmutables() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'las sugerencias no se modifican: registra una decisión o calcula otra';
END $$;
CREATE TRIGGER sugerencias_sin_cambios BEFORE UPDATE ON sugerencias
    FOR EACH ROW EXECUTE FUNCTION sugerencias_inmutables();
