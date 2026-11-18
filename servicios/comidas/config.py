"""Configuración del sistema. Cambia aquí modelos, umbrales y tus parámetros de insulina."""

OLLAMA_URL = "http://localhost:11434"

# Modelos de visión (también se usan en modo solo texto). Mejor de familias distintas.
# ~8 GB VRAM:   ["qwen3-vl:4b", "gemma3:4b", "minicpm-v"]
# 12-16 GB:     ["qwen3-vl:8b", "gemma3:12b", "minicpm-v"]
# 24 GB / Mac 32 GB+: ["qwen3-vl:8b", "gemma3:27b", "llama3.2-vision:11b"]
MODELOS = ["qwen3-vl:32b", "gemma3:27b", "minicpm-v"]  # GPU 32 GB

# Modelos para descripciones sin foto (pueden ser los mismos u otros solo-texto)
MODELOS_TEXTO = MODELOS

REPETICIONES = 3          # consultas por modelo y plato (se usa la mediana)
TEMPERATURA = 0.4
TIMEOUT = 300             # segundos por consulta

# Base de datos de alimentos
RUTA_BD = "alimentos.sqlite"            # generada con preparar_bd.py (USDA)
RUTA_PROPIOS = "alimentos_propios.csv"  # tus alimentos, etiquetas, BEDCA, recetas
UMBRAL_COINCIDENCIA = 80                # 0-100, similitud mínima del nombre

# Hidratos: USDA da hidratos totales (incluyen fibra). Si True, el bolo usa hidratos - fibra
RESTAR_FIBRA = True

# Alertas de discrepancia (sobre hidratos del plato)
ALERTA_ABS_G = 10         # gramos
ALERTA_REL = 0.20         # 20 %
ALERTA_CV_MODELO = 0.15   # inestabilidad de un modelo consigo mismo

# Parámetros personales (por defecto; editables en la app)
RATIO_HC = 10.0           # gramos de hidratos por unidad
SENSIBILIDAD = 40.0       # mg/dL que baja 1 unidad
OBJETIVO = 110.0          # mg/dL
REDONDEO_U = 0.5

RUTA_REGISTRO = "registro.jsonl"
