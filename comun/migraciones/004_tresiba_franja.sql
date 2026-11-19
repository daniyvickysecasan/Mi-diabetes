-- 004: la Tresiba se pone dentro de una franja horaria, no a una hora fija (pedido de Daniel, 19-11-2026).
-- `hora_local` pasa a ser el inicio de la franja y `hora_fin_local` su final. Las filas antiguas
-- quedan con una franja de duración cero (inicio = fin), que equivale a la hora fija de antes.
-- (La 003 está reservada para el script de la API de LibreView.)

ALTER TABLE config_tresiba ADD COLUMN hora_fin_local time;
UPDATE config_tresiba SET hora_fin_local = hora_local;
ALTER TABLE config_tresiba ALTER COLUMN hora_fin_local SET NOT NULL;
COMMENT ON COLUMN config_tresiba.hora_local IS 'Inicio de la franja horaria (hora local)';
COMMENT ON COLUMN config_tresiba.hora_fin_local IS 'Fin de la franja horaria (hora local); si es anterior al inicio, cruza la medianoche';
