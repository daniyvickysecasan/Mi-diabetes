-- 012: "Mis platos": la misma estimación para el mismo plato (fase 3).
-- Al revisar una comida con descripción, la API guarda (o actualiza) su plato con los hidratos que
-- confirmó Daniel. El trabajador de comidas y el chat lo reutilizan cuando la descripción coincide
-- (comun/platos.py, por texto, nunca un modelo). Un plato pesado (referencia) no lo pisa una comida
-- sin pesar. El valor sigue pasando por la revisión de Daniel antes de cualquier dosis.
CREATE TABLE platos (
    id             bigserial PRIMARY KEY,
    nombre         text NOT NULL,
    clave          text NOT NULL UNIQUE,              -- nombre normalizado (comun.platos.clave)
    hidratos_g     numeric(6,1) NOT NULL CHECK (hidratos_g >= 0 AND hidratos_g <= 500),
    grasa_g        numeric(6,1),
    proteina_g     numeric(6,1),
    referencia     boolean NOT NULL DEFAULT false,    -- pesado o con etiqueta
    comida_id      bigint REFERENCES comidas (id) ON DELETE SET NULL,   -- última comida que lo fijó
    veces          integer NOT NULL DEFAULT 1,
    creado_en      timestamptz NOT NULL DEFAULT now(),
    actualizado_en timestamptz NOT NULL DEFAULT now()
);

-- Comida cuya estimación salió de un plato guardado.
ALTER TABLE comidas ADD COLUMN plato_id bigint REFERENCES platos (id) ON DELETE SET NULL;
