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
    libreview_dispositivo: str = ""       # trustedDeviceToken copiado de Chrome (secreto, solo en .env)
    intervalo_pluma_s: int = 5            # cada cuánto mira la base (lectura pedida, SMS, código)
    solape_pluma_h: int = 48              # relee los dos últimos días: la pluma sube con retraso

    # Fotos de comidas (volumen local; nunca en el repositorio)
    ruta_fotos: str = "/datos/fotos"

    # Rangos (mg/dL), consenso internacional
    rango_bajo: int = 70
    rango_alto: int = 180
    zona_horaria: str = "Europe/Madrid"

    # Aviso "Lee la pluma" (comun/aviso_pluma.py)
    aviso_pluma_horas: float = 12.0       # sin dosis de la pluma durante más de esto = avisar
    aviso_pluma_margen_min: int = 45      # dosis apuntada en la app sin llegar de la pluma tras esto = avisar

    # Anomalías y avisos por ntfy (fase 4, servicios/anomalias). Umbrales: ANOMALIA_* en el .env.
    intervalo_anomalias_s: int = 300
    ntfy_url: str = ""                    # servidor ntfy de casa visto desde Docker (http://ntfy:80); vacío = no envía
    ntfy_tema: str = ""                   # tema largo y aleatorio (secreto, solo en .env)
    ntfy_token: str = ""                  # token de ntfy con permiso de escritura en el tema (secreto)
    url_app: str = ""                     # se abre al tocar el aviso (https://midiabetes.intelligentcontrol.net)
    # Web Push a la PWA (claves VAPID: python -m servicios.anomalias claves-push). Vacío = sin Web Push.
    vapid_publica: str = ""
    vapid_privada: str = ""               # secreto, solo en .env
    avisos_ntfy: str = "urgentes"         # qué va también por ntfy: urgentes, todos o ninguno

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
            libreview_dispositivo=e("LIBREVIEW_DISPOSITIVO_CONFIANZA") or "",
            intervalo_pluma_s=int(e("INTERVALO_PLUMA_S") or 5),
            solape_pluma_h=int(e("SOLAPE_PLUMA_H") or 48),
            ruta_fotos=e("RUTA_FOTOS") or "/datos/fotos",
            rango_bajo=int(e("RANGO_BAJO") or 70),
            rango_alto=int(e("RANGO_ALTO") or 180),
            zona_horaria=e("ZONA_HORARIA") or "Europe/Madrid",
            aviso_pluma_horas=float(e("AVISO_PLUMA_HORAS") or 12),
            aviso_pluma_margen_min=int(e("AVISO_PLUMA_MARGEN_MIN") or 45),
            intervalo_anomalias_s=int(e("INTERVALO_ANOMALIAS_S") or 300),
            ntfy_url=(e("NTFY_URL") or "").rstrip("/"),
            ntfy_tema=e("NTFY_TEMA") or "",
            ntfy_token=e("NTFY_TOKEN") or "",
            url_app=(e("URL_APP") or "").rstrip("/"),
            vapid_publica=e("VAPID_PUBLICA") or "",
            vapid_privada=e("VAPID_PRIVADA") or "",
            avisos_ntfy=(e("AVISOS_NTFY") or "urgentes").strip().lower(),
            rp_id=e("RP_ID") or "localhost",
            rp_nombre=e("RP_NOMBRE") or "Mi Diabetes",
            origenes=origenes,
            codigo_alta=e("CODIGO_ALTA") or "",
            cookie_segura=_bool(e("COOKIE_SEGURA"), True),
            dias_sesion=int(e("DIAS_SESION") or 30),
        )
