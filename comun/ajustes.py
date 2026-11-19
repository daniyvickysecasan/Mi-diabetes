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
    # Token de un sujeto con rol "careportal" (puede crear tratamientos). Solo lo usa el
    # recolector de la pluma para escribir sus dosis. Vacío = no escribe en Nightscout.
    nightscout_token_escritura: str = ""

    # LibreView (API no oficial). Credenciales solo en despliegue/.env del PC.
    libreview_email: str = ""
    libreview_password: str = ""
    libreview_region: str = ""            # vacío = la que diga LibreView al entrar (p. ej. "eu")
    libreview_version: str = "3.27.0.42"  # versión de la web de LibreView que se anuncia en las cabeceras
    libreview_sesion: str = "/datos/libreview/sesion.json"  # token de dispositivo de confianza (volumen local)
    intervalo_informe_s: int = 600        # cada cuánto se pide el informe a LibreView
    intervalo_pluma_s: int = 30           # cada cuánto mira la base (SMS pedido, código escrito)
    solape_pluma_h: int = 48              # relee los dos últimos días: la pluma sube con retraso

    # Rangos (mg/dL), consenso internacional
    rango_bajo: int = 70
    rango_alto: int = 180
    zona_horaria: str = "Europe/Madrid"

    # Aviso "Lee la pluma" (comun/aviso_pluma.py)
    aviso_pluma_horas: float = 12.0       # sin dosis de la pluma durante más de esto = avisar
    aviso_pluma_margen_min: int = 45      # dosis apuntada en la app sin llegar de la pluma tras esto = avisar

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
            nightscout_token_escritura=e("NIGHTSCOUT_TOKEN_ESCRITURA") or "",
            libreview_email=e("LIBREVIEW_EMAIL") or "",
            libreview_password=e("LIBREVIEW_PASSWORD") or "",
            libreview_region=(e("LIBREVIEW_REGION") or "").strip().lower(),
            libreview_version=e("LIBREVIEW_VERSION") or "3.27.0.42",
            libreview_sesion=e("LIBREVIEW_SESION") or "/datos/libreview/sesion.json",
            intervalo_informe_s=int(e("INTERVALO_INFORME_S") or 600),
            intervalo_pluma_s=int(e("INTERVALO_PLUMA_S") or 30),
            solape_pluma_h=int(e("SOLAPE_PLUMA_H") or 48),
            rango_bajo=int(e("RANGO_BAJO") or 70),
            rango_alto=int(e("RANGO_ALTO") or 180),
            zona_horaria=e("ZONA_HORARIA") or "Europe/Madrid",
            aviso_pluma_horas=float(e("AVISO_PLUMA_HORAS") or 12),
            aviso_pluma_margen_min=int(e("AVISO_PLUMA_MARGEN_MIN") or 45),
            rp_id=e("RP_ID") or "localhost",
            rp_nombre=e("RP_NOMBRE") or "Mi Diabetes",
            origenes=origenes,
            codigo_alta=e("CODIGO_ALTA") or "",
            cookie_segura=_bool(e("COOKIE_SEGURA"), True),
            dias_sesion=int(e("DIAS_SESION") or 30),
        )
