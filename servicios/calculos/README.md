# calculos

Módulo 3 (fase 2). Los cálculos deterministas son funciones puras compartidas y viven en `comun/`
(regla 3 de CLAUDE.md), para que cualquier servicio las use sin importarse entre sí:

- `comun/insulina.py`: insulina activa con la curva de Fiasp (oref0, pico 55 min, 5 h).
- `comun/metricas.py`: métricas de consenso (TBR/TIR/TAR por niveles, media, CV, GMI, cobertura).
- `comun/conciliacion.py`: conciliación app ↔ pluma y revisión por Daniel.
- `comun/tresiba.py`: configuración de Tresiba con historial y dosis programadas.

Cualquier cambio en ellos afecta a la dosis: tests y aprobación explícita de Daniel.
