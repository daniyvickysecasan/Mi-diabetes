-- 002: fase 2. Conciliación de dosis app ↔ pluma, envíos sin conexión e insulina efectiva.
--
-- Modelo: cada fila de `dosis` es una OBSERVACIÓN de una inyección por una vía (app, API de
-- LibreView, CSV de LibreView). La conciliación (comun/conciliacion.py) decide qué filas son la
-- misma inyección. La vista `dosis_efectivas` devuelve cada inyección real UNA sola vez:
--   * Las filas de la pluma (pluma_libreview, csv_libreview) cuentan salvo que estén descartadas
--     (p. ej. el CSV repite una dosis que ya trajo la API, o Daniel la descarta al revisar).
--   * Las filas de la app cuentan solo si NO están emparejadas con una de la pluma: emparejadas,
--     manda la pluma en hora y unidades; la app aporta el vínculo con la comida.

ALTER TABLE dosis
    ADD COLUMN requiere_revision   boolean NOT NULL DEFAULT false,
    ADD COLUMN motivo_conciliacion text,
    ADD COLUMN conciliado_en       timestamptz,
    ADD COLUMN revisada_en         timestamptz,          -- decisión de Daniel: congela la fila
    ADD COLUMN revision            text CHECK (revision IN ('aceptada', 'separadas', 'descartada')),
    ADD COLUMN separada_de         bigint REFERENCES dosis (id),  -- "separar": la otra mitad
    ADD COLUMN numero_serie        text;                 -- pluma que la registró, si se sabe

-- Una pareja siempre apunta a su otra mitad, y la revisión siempre deja constancia.
ALTER TABLE dosis
    ADD CONSTRAINT pareja_con_par CHECK
        (estado_conciliacion NOT IN ('emparejada', 'discrepancia') OR dosis_par_id IS NOT NULL),
    ADD CONSTRAINT sola_sin_par CHECK
        (estado_conciliacion NOT IN ('pendiente', 'solo_app', 'solo_pluma') OR dosis_par_id IS NULL),
    ADD CONSTRAINT revision_con_fecha CHECK ((revision IS NULL) = (revisada_en IS NULL)),
    ADD CONSTRAINT no_es_su_propio_par CHECK (dosis_par_id IS NULL OR dosis_par_id <> id);

-- Una dosis no puede estar emparejada con dos a la vez.
CREATE UNIQUE INDEX dosis_par_unico ON dosis (dosis_par_id)
    WHERE estado_conciliacion IN ('emparejada', 'discrepancia');
CREATE INDEX dosis_revision ON dosis (ts DESC) WHERE requiere_revision;

-- Envíos de la PWA sin conexión: el identificador que genera el móvil hace idempotente el
-- reenvío (en `dosis` va en id_externo con origen 'app', protegido por UNIQUE (origen, id_externo)).
ALTER TABLE hidratos_correccion ADD COLUMN id_cliente text UNIQUE;

-- ------------------------------------------------------------------ inyecciones reales
CREATE VIEW dosis_efectivas AS
SELECT d.id,
       d.ts,
       d.unidades,
       d.insulina,
       d.origen,
       d.estado_conciliacion,
       d.requiere_revision,
       COALESCE(app.comida_id, d.comida_id) AS comida_id,     -- la app manda en la comida
       app.id AS dosis_app_id
FROM dosis d
LEFT JOIN dosis app
       ON app.dosis_par_id = d.id
      AND app.origen NOT IN ('pluma_libreview', 'csv_libreview')
      AND app.estado_conciliacion IN ('emparejada', 'discrepancia')
WHERE d.estado_conciliacion <> 'descartada'
  AND (d.origen IN ('pluma_libreview', 'csv_libreview') OR d.dosis_par_id IS NULL);
