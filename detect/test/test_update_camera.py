"""Comprueba PATCH /cameras/{id}: editar cualquier campo conservando el resto.

No necesita placa ni GPU: las sesiones nacen paradas y aquí nunca se arrancan,
así que no se abre ningún stream ni se carga ningún modelo (salvo en el caso
de cambiar model_name, que se salta si no hay pesos descargados).

    cd detect
    venv\\Scripts\\python.exe test\\test_update_camera.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import camera  # noqa: E402
import main as _main  # noqa: E402
import registry as _registry  # noqa: E402
from servo_tracker import ServoConfig  # noqa: E402

failures: list[str] = []


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


# Que el test no pise el cameras_config.json real
_registry.CAMERAS_CONFIG_FILE = Path(tempfile.mkdtemp()) / "cameras_config.json"
_registry.CAMERAS.clear()

client = TestClient(_main.app)


def fresh(camera_id="cam", **extra):
    _registry.CAMERAS.pop(camera_id, None)
    cfg = camera.CameraConfig(
        camera_id=camera_id, stream_url="http://192.0.2.1:8080/",
        device="cpu", model_name="yolo11n", confidence=0.4, classes=[0, 16],
        default_infer=False, keepalive_enabled=True, **extra,
    )
    return _registry.register_camera(cfg)


print("\n=== 1. Sin campos no cambia nada ===")
s = fresh()
r = client.patch("/cameras/cam", data={})
check("200", r.status_code == 200, r.text)
check("ni rebuilt ni relaunched", r.json()["rebuilt"] is False and r.json()["relaunched"] is False)
check("misma sesión", _registry.CAMERAS["cam"] is s)

print("\n=== 2. Campo en caliente: misma sesión, resto intacto ===")
s = fresh()
r = client.patch("/cameras/cam", data={"confidence": 0.9, "imgsz": 320})
check("200", r.status_code == 200, r.text)
check("misma sesión", _registry.CAMERAS["cam"] is s)
check("confidence actualizada", s.cfg.confidence == 0.9)
check("imgsz actualizado", s.cfg.imgsz == 320)
check("classes intactas", s.cfg.classes == [0, 16])
check("default_infer intacto", s.cfg.default_infer is False)
check("keepalive intacto", s.cfg.keepalive_enabled is True)
check("stream_url intacta", s.cfg.stream_url == "http://192.0.2.1:8080/")
check("no rebuilt", r.json()["rebuilt"] is False)
saved = json.loads(_registry.CAMERAS_CONFIG_FILE.read_text())
check("persistido a disco", any(c["camera_id"] == "cam" and c["confidence"] == 0.9 for c in saved))

print("\n=== 3. Reenviar el mismo valor no cuenta como cambio ===")
s = fresh()
r = client.patch("/cameras/cam", data={"model_name": "yolo11n", "device": "cpu"})
check("200", r.status_code == 200, r.text)
check("no relaunched", r.json()["relaunched"] is False)
check("misma sesión", _registry.CAMERAS["cam"] is s)

print("\n=== 4. clear_* ===")
s = fresh()
r = client.patch("/cameras/cam", data={"clear_classes": True, "clear_keepalive": True})
check("200", r.status_code == 200, r.text)
check("classes -> None (todas)", s.cfg.classes is None)
check("keepalive -> None (global)", s.cfg.keepalive_enabled is None)

print("\n=== 5. Cambiar stream_url reconstruye conservando servo ===")
s = fresh(noise_psk="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", servo=ServoConfig(gain=0.5))
check("precondición: tiene servo", s.servo_tracker is not None)
shut = [0]
_orig_shutdown = s.shutdown
s.shutdown = lambda: (shut.__setitem__(0, shut[0] + 1), _orig_shutdown())
r = client.patch("/cameras/cam", data={"stream_url": "http://192.0.2.2:8080/"})
check("200", r.status_code == 200, r.text)
check("rebuilt", r.json()["rebuilt"] is True)
n = _registry.CAMERAS["cam"]
check("sesión distinta", n is not s)
check("la vieja recibió shutdown", shut[0] == 1)
check("stream_url nueva", n.cfg.stream_url == "http://192.0.2.2:8080/")
check("servo conservado", n.cfg.servo is not None and n.cfg.servo.gain == 0.5)
check("servo_tracker montado en la nueva", n.servo_tracker is not None)
check("esphome apunta al host nuevo", n.esphome is not None and n.esphome.address == "192.0.2.2")
check("confidence conservada", n.cfg.confidence == 0.4)
check("sigue parada", not n.is_running)
n.shutdown()

print("\n=== 6. clear_noise_psk quita la API de ESPHome ===")
s = fresh(noise_psk="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
check("precondición: tiene esphome", s.esphome is not None)
r = client.patch("/cameras/cam", data={"clear_noise_psk": True})
check("200", r.status_code == 200, r.text)
n = _registry.CAMERAS["cam"]
check("rebuilt", r.json()["rebuilt"] is True)
check("sin esphome", n.esphome is None and n.cfg.noise_psk is None)

print("\n=== 7. Renombrar ===")
s = fresh()
fresh("otra")
r = client.patch("/cameras/cam", data={"new_camera_id": "otra"})
check("400 si el id nuevo ya existe", r.status_code == 400, r.text)
check("y cam sigue donde estaba", _registry.CAMERAS.get("cam") is s)
r = client.patch("/cameras/cam", data={"new_camera_id": "renombrada"})
check("200", r.status_code == 200, r.text)
check("aparece con el id nuevo", "renombrada" in _registry.CAMERAS)
check("desaparece el viejo", "cam" not in _registry.CAMERAS)
check("cfg.camera_id actualizado", _registry.CAMERAS["renombrada"].cfg.camera_id == "renombrada")
saved = json.loads(_registry.CAMERAS_CONFIG_FILE.read_text())
check("persistido con el id nuevo", [c["camera_id"] for c in saved].count("renombrada") == 1
      and "cam" not in [c["camera_id"] for c in saved])
_registry.CAMERAS.pop("renombrada", None)
_registry.CAMERAS.pop("otra", None)

print("\n=== 8. 404 si no existe ===")
r = client.patch("/cameras/noexiste", data={"confidence": 0.5})
check("404", r.status_code == 404, r.text)

print("\n=== 9. device inválido -> 400 sin tocar nada ===")
s = fresh()
r = client.patch("/cameras/cam", data={"device": "tpu", "confidence": 0.99})
check("400", r.status_code == 400, r.text)
check("confidence no cambió", s.cfg.confidence == 0.4)
_registry.CAMERAS.pop("cam", None)

print()
if failures:
    print(f"{len(failures)} FALLO(S): {failures}")
    sys.exit(1)
print("todo OK")
