"""Marca de origen propio en Nightscout.

Todo lo que este sistema escriba en Nightscout (fase 2: dosis leídas de LibreView) lleva
`enteredBy` empezando por ORIGEN_PROPIO. El recolector lo ignora al leer para no volver a
importar lo que ya está en la base (bucle de duplicados).
"""

ORIGEN_PROPIO = "mi-diabetes"


def es_origen_propio(tratamiento: dict) -> bool:
    """True si el tratamiento de Nightscout lo escribió este sistema."""
    por = str(tratamiento.get("enteredBy") or "").strip().lower()
    return por.startswith(ORIGEN_PROPIO) or tratamiento.get("origen_mi_diabetes") is True
