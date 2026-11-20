"""Prueba del registro tipo chat con el modelo real (criterio de la fase 3: ≥ 9 de cada 10 bien).

  python -m herramientas.frases_chat [--url http://localhost:11434] [--modelo gemma3:27b]

Frases inventadas (sin datos reales). Una frase cuenta como bien si el tipo coincide y la hora de
inicio cae en el margen esperado. No escribe nada en la base.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from servicios.comidas import chat as CH

# (frase, tipo esperado, minutos desde el inicio hasta ahora: (mín, máx) o None si da igual)
FRASES = [
    ("me estoy tomando unas cervezas con los amigos", "alcohol", (0, 30)),
    ("acabo de salir a correr, unos 40 minutos", "ejercicio", (0, 15)),
    ("voy a ir al gimnasio en media hora", "ejercicio", (-45, -15)),
    ("he comido una paella hace una hora", "comida", (45, 90)),
    ("me he tomado un zumo de naranja para subir", "bebida", (0, 20)),
    ("estoy con fiebre desde esta mañana", "enfermedad", None),
    ("he dormido fatal, solo 4 horas", "sueno", None),
    ("reunión muy tensa en el trabajo", "estres", None),
    ("dos copas de vino en la cena", "alcohol", None),
    ("paseo de una hora con el perro", "ejercicio", None),
    ("un café con leche de avena", "bebida", (0, 20)),
    ("bocadillo de jamón ahora mismo", "comida", (0, 15)),
    ("me duele la garganta y estoy resfriado", "enfermedad", None),
    ("partido de pádel a las 7 de la tarde durante hora y media", "ejercicio", None),
    ("me voy a echar la siesta", "sueno", (-10, 10)),
    ("un gin tonic", "alcohol", (0, 20)),
    ("examen mañana, estoy nervioso", "estres", None),
    ("hace 20 minutos me he comido una manzana", "comida", (10, 30)),
    ("subo andando 10 pisos de escaleras", "ejercicio", (0, 15)),
    ("estoy en un cumpleaños, tarta y refrescos", "comida", (0, 30)),
]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:11434")
    p.add_argument("--modelo", default="gemma3:27b")
    a = p.parse_args(argv)
    ahora = datetime.now(timezone.utc)
    bien = 0
    for frase, tipo, margen in FRASES:
        try:
            d = CH.normalizar(CH.consultar(a.url, a.modelo, frase), ahora)
        except Exception as e:
            print(f"ERROR  {frase!r}: {e}")
            continue
        hace = (ahora - d["ts_evento_inicio"]) / timedelta(minutes=1) if d["ts_evento_inicio"] else None
        ok = d["tipo"] == tipo and (margen is None or (hace is not None and margen[0] <= hace <= margen[1]))
        bien += ok
        print(f"{'bien ' if ok else 'MAL  '} {frase!r}: {d['tipo']} (esperado {tipo}), "
              f"hace {hace if hace is None else round(hace)} min, {d['estimacion']}, {d['incertidumbre']}")
    print(f"\n{bien}/{len(FRASES)} bien ({bien / len(FRASES):.0%}); criterio ≥ 90 %")
    return 0 if bien >= 0.9 * len(FRASES) else 1


if __name__ == "__main__":
    sys.exit(main())
