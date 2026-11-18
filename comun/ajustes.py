"""Ajustes leídos de variables de entorno. Nada de secretos en el código.

Plantilla con todas las variables: despliegue/.env.example
"""
import os
from dataclasses import dataclass, field


def _bool(valor: str | None, defecto: bool) -> bool:
    if valor is None or valor == "":
        return defecto
    return valor.strip().lower() in ("1", "true", "si", "sí", "yes")


@dataclass(frozen=True)
class Ajustes:
    database_url: str = "postgresql://midiabetes:midiabetes@localhost:5432/midiabetes"

    # Nightscout (token de SOLO LECTURA: rol "readable")
    nightscout_url: str = ""
    nightscout_token: str = ""
    intervalo_recoleccion_s: int = 300
    solape_incremental_min: int = 180     # relee las últimas 3 h por si LibreLinkUp rellena tarde
    hueco_min: int = 20                   # sin lecturas durante más de esto = hueco

    # Rangos (mg/dL), consenso internacional
    rango_bajo: int = 70
    rango_alto: int = 180
    zona_horaria: str = "Europe/Madrid"

    # Passkey / sesión
    rp_id: str = "localhost"
    rp_nombre: str = "Mi Diabetes"
    origenes: tuple[str, ...] = field(default=("http://localhost:8000",))
    codigo_alta: str = ""                 # obligatorio para registrar una passkey
    cookie_segura: bool = True
    dias_sesion: int = 30

    @classmethod
    def desde_entorno(cls) -> "Ajustes":
        e = os.environ.get
        origenes = tuple(o.strip() for o in (e("ORIGENES") or "http://localhost:8000").split(",") if o.strip())
        return cls(
            database_url=e("DATABASE_URL") or cls.database_url,
            nightscout_url=(e("NIGHTSCOUT_URL") or "").rstrip("/"),
            nightscout_token=e("NIGHTSCOUT_TOKEN") or "",
            intervalo_recoleccion_s=int(e("INTERVALO_RECOLECCION_S") or 300),
            solape_incremental_min=int(e("SOLAPE_INCREMENTAL_MIN") or 180),
            hueco_min=int(e("HUECO_MIN") or 20),
            rango_bajo=int(e("RANGO_BAJO") or 70),
            rango_alto=int(e("RANGO_ALTO") or 180),
            zona_horaria=e("ZONA_HORARIA") or "Europe/Madrid",
            rp_id=e("RP_ID") or "localhost",
            rp_nombre=e("RP_NOMBRE") or "Mi Diabetes",
            origenes=origenes,
            codigo_alta=e("CODIGO_ALTA") or "",
            cookie_segura=_bool(e("COOKIE_SEGURA"), True),
            dias_sesion=int(e("DIAS_SESION") or 30),
        )
