-- 007: decisión de Daniel sobre cada sugerencia (hecho / modificado / ignorado) y su resultado a 3–4 h.
-- Una sola decisión por sugerencia. Si se pone insulina, la dosis va a `dosis` (origen 'app') y
-- pasa por la conciliación con la pluma como cualquier otra: la pluma manda en hora y unidades.

CREATE UNIQUE INDEX decisiones_una_por_sugerencia ON decisiones (sugerencia_id);

ALTER TABLE decisiones
    ADD COLUMN id_cliente text UNIQUE,                 -- reenvío idempotente desde el móvil
    ADD CONSTRAINT unidades_segun_decision CHECK (
        (decision = 'ignorado' AND unidades_reales IS NULL AND dosis_id IS NULL)
        OR (decision IN ('hecho', 'modificado') AND unidades_reales IS NOT NULL AND unidades_reales >= 0)),
    ADD CONSTRAINT resultado_con_fecha CHECK ((resultado IS NULL) = (evaluado_en IS NULL));
