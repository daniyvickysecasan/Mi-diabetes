# calculos

Módulo 3 (fase 2). Los cálculos deterministas son funciones puras compartidas y viven en `comun/`
(regla 3 de CLAUDE.md), para que cualquier servicio las use sin importarse entre sí:

- `comun/insulina.py`: insulina activa con la curva de Fiasp (oref0, pico 55 min, 5 h).
- `comun/metricas.py`: métricas de consenso (TBR/TIR/TAR por niveles, media, CV, GMI, cobertura).
- `comun/conciliacion.py`: conciliación app ↔ pluma y revisión por Daniel.
- `comun/tresiba.py`: configuración de Tresiba con historial y dosis programadas.

- `comun/parametros.py`: versiones de ratio y sensibilidad por franja; solo se activan con aprobación
  (`python -m comun.parametros proponer|aprobar|listar`).
- `comun/sugerencia.py`: cálculo puro de la sugerencia de Fiasp (fase 3): hidratos + corrección −
  insulina activa, freno por hipoglucemia, alerta por acumulación, segunda inyección por grasa y
  proteína (Varsovia) e indicador de incertidumbre. `comun/sugerir.py` reúne las entradas de la base
  (incluida la lectura de la pluma antes del cálculo) y guarda la sugerencia con su traza.

Cualquier cambio en ellos afecta a la dosis: tests y aprobación explícita de Daniel.
