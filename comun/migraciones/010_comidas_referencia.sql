-- 010: comidas de referencia para medir el error de los modelos (aceptación de la fase 3).
-- Al revisar, Daniel puede marcar que ha pesado la comida o que tiene etiqueta: entonces sus
-- hidratos son la referencia. Se guarda aparte lo que estimaron los modelos, para no perderlo
-- al corregir.
ALTER TABLE comidas
    ADD COLUMN referencia        boolean NOT NULL DEFAULT false,
    ADD COLUMN hidratos_modelo_g numeric(6,1);

UPDATE comidas SET hidratos_modelo_g = hidratos_bolo_g
 WHERE estado_analisis = 'lista' AND NOT corregida AND hidratos_bolo_g IS NOT NULL;
