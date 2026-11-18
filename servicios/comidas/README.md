# Hidratos por foto o descripción (local, sin suscripciones)

Herramienta de pruebas personal. No sustituye a tu cálculo habitual ni al criterio médico.

## Cómo funciona
1. Cada foto (o la descripción, si no hay foto) se manda a varios modelos locales en Ollama,
   `REPETICIONES` veces a cada uno.
2. Cada modelo devuelve alimentos, gramos y su estimación de nutrientes.
3. Dos vías por modelo:
   - **BD**: los gramos se cruzan con la base local (tus alimentos propios primero, luego USDA).
   - **modelo**: la estimación directa del modelo.
4. Consenso = mediana de todas las vías. Hay alerta si:
   - la dispersión supera 10 g o el 20 %,
   - alguna estimación se aleja del consenso,
   - un modelo se contradice consigo mismo,
   - un alimento no está en la base.
5. Revisas y corriges la tabla, se calcula la comida completa y el bolo de referencia
   (hidratos + corrección − insulina activa, y grasa/proteína por el método de Varsovia).
6. "Guardar en registro" añade todo a `registro.jsonl`. Con el tiempo te sirve para ver qué modelo acierta más.

## Instalación
1. Instala Ollama: https://ollama.com
2. Descarga los modelos de `config.py` (ajústalos a tu memoria de vídeo):
   ```
   ollama pull qwen3-vl:32b
   ollama pull gemma3:27b
   ollama pull minicpm-v
   ```
3. Instala las dependencias de Python (3.10 o superior):
   ```
   pip install requests rapidfuzz pandas gradio
   ```
4. Base de alimentos USDA (gratuita): descarga "SR Legacy" en CSV desde
   https://fdc.nal.usda.gov/download-datasets, descomprime y ejecuta:
   ```
   python preparar_bd.py ruta/carpeta_descomprimida
   ```
5. Arranca la interfaz:
   ```
   python app.py
   ```
   Ábrela en http://localhost:7860, o desde el móvil en la misma wifi con http://IP-DEL-ORDENADOR:7860.

También funciona por terminal:
```
python nutricion.py --foto plato.jpg --foto postre.jpg --nota "arroz integral" --glucosa 160
python nutricion.py --texto "menú del día: lentejas con chorizo, filete con patatas fritas y flan"
```

## Tus alimentos (`alimentos_propios.csv`)
Se consultan antes que USDA. Valores **por 100 g**. Los hidratos van como en la etiqueta europea,
**sin fibra**: el programa suma la fibra para guardarlos como totales, igual que USDA.
En `alias` pon otros nombres separados por `|`.
Las dos filas que trae el archivo son ejemplos: cámbialas por los datos de tus etiquetas,
de BEDCA (https://www.bedca.net) o de tus recetas.

## Consejos para mejores estimaciones
- Haz la foto cenital y con un objeto de tamaño conocido (tenedor, tarjeta).
- Una foto por plato. Si lo sabes, pon en la nota el peso o el tamaño del plato.
- Para comer fuera, describe cantidades si puedes ("media ración", "plato hondo lleno").
- Con `RESTAR_FIBRA = True` el bolo usa hidratos − fibra, como en las etiquetas europeas.
- Más repeticiones y modelos aumentan la fiabilidad, pero también el tiempo de espera.
