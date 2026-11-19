-- 008: análisis de comidas por foto o texto (fase 3, tareas 1–2).
-- La API guarda la foto (volumen local, nunca en el repositorio) y deja la comida 'pendiente';
-- el trabajador de comidas (servicios/comidas/trabajador.py) la analiza con los modelos locales
-- y la deja 'lista'. Solo se comunican por esta tabla (regla 3).
-- Los gramos que estiman los modelos NO entran en una dosis hasta que Daniel revisa la comida
-- (`revisada`): comun/sugerir.py lo exige.

ALTER TABLE comidas
    ADD COLUMN estado_analisis text NOT NULL DEFAULT 'manual'
        CHECK (estado_analisis IN ('manual', 'pendiente', 'analizando', 'lista', 'error')),
    ADD COLUMN error_analisis  text,
    ADD COLUMN analizada_en    timestamptz,
    ADD COLUMN revisada_en     timestamptz,
    ADD COLUMN corregida       boolean NOT NULL DEFAULT false,   -- Daniel cambió algún valor al revisar
    ADD COLUMN id_cliente      text UNIQUE,
    ADD CONSTRAINT revisada_con_fecha CHECK (NOT revisada OR revisada_en IS NOT NULL),
    ADD CONSTRAINT rango_coherente CHECK (
        rango_hidratos_min_g IS NULL OR rango_hidratos_max_g IS NULL OR rango_hidratos_min_g <= rango_hidratos_max_g);

CREATE INDEX comidas_por_analizar ON comidas (creado_en) WHERE estado_analisis = 'pendiente';
