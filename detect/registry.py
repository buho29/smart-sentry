"""Alta, baja y persistencia de las camaras registradas.

La lista vive en memoria de un unico proceso (por eso uvicorn va con
--workers 1) y se vuelca a cameras_config.json para sobrevivir a un reinicio.
"""

import json
import threading
from pathlib import Path

from fastapi import HTTPException

from camera import CameraConfig, CameraSession
from log import print


# Dónde se guarda la lista de cámaras registradas para que sobrevivan a un reinicio
CAMERAS_CONFIG_FILE = Path(__file__).with_name("cameras_config.json")


# ---------------------------------------------------------------------------
# Registro de cámaras, persistido en CAMERAS_CONFIG_FILE para sobrevivir a
# un reinicio del servicio (se recargan solas al arrancar)
# ---------------------------------------------------------------------------

CAMERAS: dict[str, CameraSession] = {}
_cameras_lock = threading.Lock()


def save_cameras_to_disk():
    with _cameras_lock:
        data = [s.cfg.dict() for s in CAMERAS.values()]
    try:
        CAMERAS_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except OSError as e:
        print(f"No se pudo guardar {CAMERAS_CONFIG_FILE}: {e!r}")


def load_cameras_from_disk():
    if not CAMERAS_CONFIG_FILE.exists():
        return
    try:
        data = json.loads(CAMERAS_CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"No se pudo leer {CAMERAS_CONFIG_FILE}: {e!r}")
        return
    for entry in data:
        try:
            register_camera(CameraConfig(**entry), persist=False)
        except Exception as e:
            print(f"Cámara inválida en {CAMERAS_CONFIG_FILE}: {entry!r} ({e!r})")
    if data:
        print(f"{len(data)} cámara(s) cargada(s) desde {CAMERAS_CONFIG_FILE}")


def register_camera(cfg: CameraConfig, persist: bool = True) -> CameraSession:
    with _cameras_lock:
        if cfg.camera_id in CAMERAS:
            raise HTTPException(400, f"La cámara '{cfg.camera_id}' ya existe")
        session = CameraSession(cfg)
        CAMERAS[cfg.camera_id] = session
    if persist:
        save_cameras_to_disk()
    return session


def replace_camera(old_id: str, new_cfg: CameraConfig) -> tuple[CameraSession, CameraSession]:
    """Sustituye la sesión `old_id` por una nueva construida con `new_cfg`.

    Hace falta cuando cambia algo que CameraSession solo resuelve en su
    constructor (stream_url, noise_psk, esphome_state_object_id) o el propio
    id, que es la clave del registro. Devuelve (vieja, nueva): el shutdown()
    de la vieja lo hace quien llama, porque bloquea varios segundos y aquí
    estamos bajo el lock. Tampoco persiste: el endpoint guarda al final.
    """
    with _cameras_lock:
        old = CAMERAS.get(old_id)
        if old is None:
            raise HTTPException(404, f"Cámara '{old_id}' no registrada")
        if new_cfg.camera_id != old_id and new_cfg.camera_id in CAMERAS:
            raise HTTPException(400, f"La cámara '{new_cfg.camera_id}' ya existe")
        CAMERAS.pop(old_id)
        new = CameraSession(new_cfg)
        CAMERAS[new_cfg.camera_id] = new
    return old, new


def get_camera(camera_id: str) -> CameraSession:
    session = CAMERAS.get(camera_id)
    if session is None:
        raise HTTPException(404, f"Cámara '{camera_id}' no registrada")
    return session
