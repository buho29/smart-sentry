"""Comprueba los endpoints de grabación contra la app real, sin placa ni GPU.

Las sesiones nacen paradas y aquí nunca se arrancan, así que no hace falta
hardware: lo que se prueba es el contrato HTTP. Hay dos cosas que solo se
pueden cazar aquí y que son fallos silenciosos:

  - que las rutas fijas (/recordings/stats y compañía) ganen al comodín
    {clip_id:path}, porque si no, pedir las estadísticas devuelve un 404 de
    "clip no encontrado";
  - que la descarga responda a `Range` con un 206, que es lo que necesita el
    reproductor de Home Assistant para hacer seek sin bajarse el clip entero.

    cd detect
    venv\\Scripts\\python.exe test\\test_recording_api.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import camera  # noqa: E402
import clips as _clips  # noqa: E402
import main as _main  # noqa: E402
import registry as _registry  # noqa: E402

failures: list[str] = []


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


# Nada de esto puede tocar los ficheros reales del servicio. Son TRES: la lista
# de cámaras, los ajustes de almacenamiento (que este test cambia al probar la
# retención) y la propia carpeta de clips. Olvidar el segundo dejaba al servicio
# con una retención de menos de un segundo apuntando a un directorio temporal.
_registry.CAMERAS_CONFIG_FILE = Path(tempfile.mkdtemp()) / "cameras_config.json"
_registry.CAMERAS.clear()
CFGDIR = Path(tempfile.mkdtemp(prefix="apitest-cfg-"))
_clips.RECORDINGS_CONFIG_FILE = CFGDIR / "recordings_config.json"
ROOT = Path(tempfile.mkdtemp(prefix="apitest-"))
_clips.STORE.cfg = _clips.RecordingsConfig(root_dir=str(ROOT), max_age_days=0,
                                           max_total_gb=0, min_free_gb=0)

client = TestClient(_main.app)


def fresh(camera_id="cam"):
    _registry.CAMERAS.pop(camera_id, None)
    return _registry.register_camera(camera.CameraConfig(
        camera_id=camera_id, stream_url="http://192.0.2.1:8080/",
        device="cpu", model_name="yolo11n", always_infer=False,
    ))


def fake_clip(camera_id="cam", trigger="detection", ts=None, mb=0.1, note=None):
    """Un clip ya cerrado, escrito a mano como lo dejaría el grabador."""
    ts = time.time() if ts is None else ts
    p = _clips.STORE.new_clip_path(camera_id, trigger, ts=ts)
    p.write_bytes(b"\x00" * int(mb * 1024 ** 2))
    _clips.write_sidecar(p, {  # noqa: E128
        "clip_id": _clips.STORE.clip_id_of(p), "camera_id": camera_id,
        "trigger": trigger, "started_at": ts, "duration_sec": 10.0, "note": note,
        "labels": {"cat": 3}, "playable_in_browser": True,
    })
    p.with_suffix(".jpg").write_bytes(b"\xff\xd8\xff\xd9")
    # El mtime tiene que ser el del clip, no el de ahora: la retención decide
    # por mtime, y sin esto un clip "de hace una hora" se vería recién hecho.
    os.utime(p, (ts, ts))
    return p


# ---------------------------------------------------------------------------
print("\n=== 1. Configurar la grabación ===")

s = fresh()
check("una cámara nace sin grabación", s.clip_recorder is None)

r = client.post("/cameras/cam/record/start", data={})
check("sin grabación configurada, /record/start da 400", r.status_code == 400,
      f"({r.status_code})")
check("y el error dice cómo arreglarlo", "config/recording" in r.text)

r = client.post("/cameras/cam/config/recording",
                data={"source": "raw", "min_hits": 1})
check("POST /config/recording responde 200", r.status_code == 200, r.text[:150])
check("y monta el grabador", s.clip_recorder is not None)
check("sale en /status bajo consumers",
      "ClipRecorder" in r.json()["consumers"], f"({list(r.json()['consumers'])})")

saved = json.loads(_registry.CAMERAS_CONFIG_FILE.read_text())
check("y se persiste en cameras_config.json",
      saved[0].get("recording", {}).get("source") == "raw",
      f"({saved[0].get('recording')})")

r = client.post("/cameras/cam/config/recording", data={"source": "chorizo"})
check("un source inválido da 422", r.status_code == 422, f"({r.status_code})")
r = client.post("/cameras/nope/config/recording", data={})
check("una cámara inexistente da 404", r.status_code == 404)

r = client.get("/cameras/cam/record/status")
check("/record/status devuelve el estado del grabador",
      r.status_code == 200 and r.json()["state"] == "idle", r.text[:120])

# Reconfigurar en caliente no debe crear un segundo grabador.
antes = s.clip_recorder
client.post("/cameras/cam/config/recording", data={"source": "raw", "min_hits": 3})
check("reconfigurar reutiliza el mismo grabador", s.clip_recorder is antes)
check("y aplica el cambio", s.clip_recorder.cfg.min_hits == 3)
check("sin duplicar consumidores",
      sum(1 for c in s._consumers if type(c).__name__ == "ClipRecorder") == 1)


# ---------------------------------------------------------------------------
print("\n=== 2. Grabación manual sin cámara viva ===")

r = client.post("/cameras/cam/record/start", data={"note": "prueba"})
check("sin frames, /record/start da 503 en vez de colgarse", r.status_code == 503,
      f"({r.status_code} {r.text[:100]})")

r = client.post("/cameras/cam/record/stop")
check("/record/stop sin grabación da 409", r.status_code == 409, f"({r.status_code})")


# ---------------------------------------------------------------------------
print("\n=== 3. Listar clips ===")

t0 = time.time()
c1 = fake_clip("cam", "detection", ts=t0 - 3600)
c2 = fake_clip("cam", "manual", ts=t0 - 60, note="a mano")
c3 = fake_clip("porche", "detection", ts=t0 - 10)

r = client.get("/recordings")
body = r.json()
check("lista los tres clips", body["total"] == 3, f"({body['total']})")
check("cada clip trae su url de descarga",
      all(c["url"].startswith("/recordings/") for c in body["clips"]))
check("y la de la miniatura", all(c["thumbnail_url"] for c in body["clips"]))
check("más recientes primero",
      body["clips"][0]["camera_id"] == "porche", f"({body['clips'][0]['camera_id']})")

check("filtra por cámara", client.get("/recordings?camera_id=cam").json()["total"] == 2)
check("filtra por disparo", client.get("/recordings?trigger=manual").json()["total"] == 1)
check("filtra por since",
      client.get(f"/recordings?since={t0 - 600}").json()["total"] == 2,
      f"({client.get(f'/recordings?since={t0 - 600}').json()['total']})")
check("filtra por until", client.get(f"/recordings?until={t0 - 600}").json()["total"] == 1)
check("acepta fechas ISO-8601",
      client.get("/recordings?since=2020-01-01T00:00").status_code == 200)
check("una fecha ininteligible da 422",
      client.get("/recordings?since=el+martes").status_code == 422)

r = client.get("/recordings?limit=1&offset=1")
check("pagina", len(r.json()["clips"]) == 1 and r.json()["total"] == 3)
check("y el total es el de ANTES de paginar", r.json()["total"] == 3)
check("order=asc invierte",
      client.get("/recordings?order=asc").json()["clips"][0]["camera_id"] == "cam")


# ---------------------------------------------------------------------------
print("\n=== 4. Las rutas fijas ganan al comodín ===")

r = client.get("/recordings/stats")
check("/recordings/stats NO se interpreta como un clip llamado 'stats'",
      r.status_code == 200 and "total_gb" in r.json(), f"({r.status_code} {r.text[:80]})")
check("y cuenta los clips", r.json()["clips"] == 3, f"({r.json()['clips']})")

r = client.get("/recordings/capabilities")
check("/recordings/capabilities responde", r.status_code == 200, r.text[:80])
check("y dice si hay H.264", "h264" in r.json(), r.text[:120])

r = client.get("/recordings/config")
check("/recordings/config responde", r.status_code == 200 and "max_age_days" in r.json())

r = client.post("/recordings/sweep", data={"dry_run": "true"})
check("/recordings/sweep responde", r.status_code == 200 and "deleted" in r.json())


# ---------------------------------------------------------------------------
print("\n=== 5. Descargar un clip ===")

cid = _clips.STORE.clip_id_of(c1)
r = client.get(f"/recordings/{cid}")
check("descarga el MP4", r.status_code == 200, f"({r.status_code})")
check("con el content-type correcto",
      r.headers["content-type"] == "video/mp4", f"({r.headers.get('content-type')})")

# Sin esto, Home Assistant tendría que bajarse el clip entero para saltar a un
# minuto concreto.
r = client.get(f"/recordings/{cid}", headers={"Range": "bytes=0-99"})
check("responde 206 a una petición Range", r.status_code == 206, f"({r.status_code})")
check("y devuelve solo ese trozo", len(r.content) == 100, f"({len(r.content)} bytes)")

r = client.get(f"/recordings/{cid}/thumbnail")
check("sirve la miniatura",
      r.status_code == 200 and r.headers["content-type"] == "image/jpeg",
      f"({r.status_code})")

check("un clip inexistente da 404",
      client.get("/recordings/cam/2020-01-01/nada.mp4").status_code == 404)
for malo in ("../../../main.py", "cam/../../../main.py", "../cameras_config.json"):
    check(f"path traversal bloqueado ({malo})",
          client.get(f"/recordings/{malo}").status_code == 404,
          f"({client.get(f'/recordings/{malo}').status_code})")


# ---------------------------------------------------------------------------
print("\n=== 6. Borrar ===")

cid2 = _clips.STORE.clip_id_of(c2)
r = client.delete(f"/recordings/{cid2}")
check("DELETE borra el clip", r.status_code == 200 and not c2.is_file(), r.text[:120])
check("y también su sidecar y su miniatura",
      not c2.with_suffix(".json").is_file() and not c2.with_suffix(".jpg").is_file())
check("ya no sale en el listado", client.get("/recordings").json()["total"] == 2)
check("borrarlo otra vez da 404",
      client.delete(f"/recordings/{cid2}").status_code == 404)

_clips.STORE.mark_in_progress(cid)
check("no se puede borrar el clip que se está grabando",
      client.delete(f"/recordings/{cid}").status_code == 409)
_clips.STORE.unmark_in_progress(cid)


# ---------------------------------------------------------------------------
print("\n=== 7. Retención por API ===")

r = client.post("/recordings/config", data={"max_age_days": 0.00001})
check("cambiar la config responde 200", r.status_code == 200, r.text[:120])
check("y se aplica al almacén", _clips.STORE.cfg.max_age_days == 0.00001)

r = client.post("/recordings/config", data={"crf": 28, "encoder": "opencv"})
check("la codificación se cambia en /recordings/config",
      r.status_code == 200 and _clips.STORE.cfg.crf == 28
      and _clips.STORE.cfg.encoder == "opencv", r.text[:120])
check("sin tocar lo que no se manda", _clips.STORE.cfg.max_age_days == 0.00001)
r = client.post("/recordings/config", data={"encoder": "chorizo"})
check("un encoder inválido da 422", r.status_code == 422, f"({r.status_code})")

r = client.post("/recordings/sweep", data={"dry_run": "true"})
n = len(r.json()["deleted"])
check("dry_run dice qué borraría", n == 2, f"({n})")
check("pero no borra", client.get("/recordings").json()["total"] == 2)

r = client.post("/recordings/sweep", data={"dry_run": "false"})
check("el barrido de verdad sí borra",
      client.get("/recordings").json()["total"] == 0, f"({r.json()})")

r = client.post("/recordings/config", data={"root_dir": str(ROOT / "otra")})
check("cambiar root_dir avisa de que lo viejo se queda atrás",
      "warning" in r.json(), f"({r.json()})")

# La guarda que faltaba: este test cambia la retención, y si escribiera en el
# fichero real dejaría al servicio con la configuración de juguete de aquí
# (retención de menos de un segundo apuntando a un directorio temporal).
check("los ajustes se han guardado en el fichero temporal",
      _clips.RECORDINGS_CONFIG_FILE.is_file(),
      f"({_clips.RECORDINGS_CONFIG_FILE})")
check("y NO se ha tocado el recordings_config.json del servicio",
      not (Path(__file__).resolve().parent.parent / "recordings_config.json").exists(),
      "(un test ha pisado la configuración real)")

# Dejar el almacén como estaba para no sorprender a otro test del mismo proceso.
_clips.STORE.cfg = _clips.RecordingsConfig(root_dir=str(ROOT), max_age_days=0,
                                           max_total_gb=0, min_free_gb=0)
shutil.rmtree(ROOT, ignore_errors=True)

print("\n" + ("TODO OK" if not failures else f"FALLOS: {failures}"))
sys.exit(1 if failures else 0)
