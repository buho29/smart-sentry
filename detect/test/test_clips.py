"""Comprueba el almacén de clips y la retención, sin grabar ni un solo vídeo.

`clips.py` no sabe nada de detecciones ni de encoders: solo de ficheros. Así que
aquí se fabrican clips falsos (unos bytes cualesquiera con un mtime puesto a
mano) en un directorio temporal y se comprueba a qué se le aplica la tijera.

Lo que de verdad importa que esté bien es `plan_deletions`, que es pura, y
`ClipStore.resolve`, que es la única barrera contra el path traversal en una API
que sirve ficheros por ruta.

    cd detect
    venv\\Scripts\\python.exe test\\test_clips.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clips import (  # noqa: E402
    PART_SUFFIX, ClipEntry, ClipStore, RecordingsConfig,
    part_path, plan_deletions, write_sidecar,
)

failures: list[str] = []

DAY = 86400.0
NOW = 1_700_000_000.0


def check(name, cond, extra=""):
    print(f"  {'PASS ' if cond else 'FALLO'}  {name} {extra}")
    if not cond:
        failures.append(name)


def entry(clip_id, age_days, mb):
    """Un ClipEntry de mentira: plan_deletions no toca el disco."""
    return ClipEntry(clip_id=clip_id, path=Path(clip_id),
                     size=int(mb * 1024 ** 2), mtime=NOW - age_days * DAY)


def make_store(**cfg) -> tuple[ClipStore, Path]:
    root = Path(tempfile.mkdtemp(prefix="cliptest-"))
    cfg.setdefault("root_dir", str(root))
    return ClipStore(RecordingsConfig(**cfg)), root


def make_clip(store, camera_id, trigger="detection", ts=None, mb=1.0,
              sidecar=True, thumbnail=True):
    """Escribe un clip completo (mp4 + json + jpg) y devuelve su ruta."""
    p = store.new_clip_path(camera_id, trigger, ts=ts)
    p.write_bytes(b"\x00" * int(mb * 1024 ** 2))
    if ts is not None:
        os.utime(p, (ts, ts))
    if sidecar:
        write_sidecar(p, {"clip_id": store.clip_id_of(p), "camera_id": camera_id,
                          "trigger": trigger})
    if thumbnail:
        p.with_suffix(".jpg").write_bytes(b"\xff\xd8\xff\xd9")
    return p


# ---------------------------------------------------------------------------
print("\n-- plan_deletions: la decisión, sin disco --")

viejos = [entry("a", 30, 1), entry("b", 20, 1), entry("c", 1, 1)]
doomed = plan_deletions(viejos, max_age_days=14, max_total_bytes=0, now=NOW)
check("caduca por edad lo que pasa del límite",
      [e.clip_id for e in doomed] == ["a", "b"], f"({[e.clip_id for e in doomed]})")

check("sin límite de edad (0) no caduca nada",
      plan_deletions(viejos, max_age_days=0, max_total_bytes=0, now=NOW) == [])

gordos = [entry("a", 5, 10), entry("b", 4, 10), entry("c", 3, 10), entry("d", 2, 10)]
doomed = plan_deletions(gordos, max_age_days=0, max_total_bytes=25 * 1024 ** 2, now=NOW)
check("por tamaño borra los MÁS ANTIGUOS primero",
      [e.clip_id for e in doomed] == ["a", "b"], f"({[e.clip_id for e in doomed]})")
check("y para en cuanto baja del tope",
      sum(e.size for e in gordos) - sum(e.size for e in doomed) <= 25 * 1024 ** 2)

check("sin límite de tamaño (0) no borra nada",
      plan_deletions(gordos, max_age_days=0, max_total_bytes=0, now=NOW) == [])

# Lo caducado no debe contar para el tope de tamaño: si contara, se borrarían
# clips recientes para hacer hueco a otros que se van a borrar en esta misma
# pasada.
mixto = [entry("viejo", 30, 50), entry("a", 3, 10), entry("b", 2, 10)]
doomed = plan_deletions(mixto, max_age_days=14, max_total_bytes=25 * 1024 ** 2, now=NOW)
check("edad primero y tamaño sobre lo que queda",
      [e.clip_id for e in doomed] == ["viejo"], f"({[e.clip_id for e in doomed]})")

doomed = plan_deletions(gordos, max_age_days=0, max_total_bytes=25 * 1024 ** 2,
                        keep={"a"}, now=NOW)
check("keep protege el clip en curso aunque sea el más antiguo",
      "a" not in [e.clip_id for e in doomed], f"({[e.clip_id for e in doomed]})")

check("lista vacía no revienta", plan_deletions([], 14, 1024, now=NOW) == [])

viejo_y_gordo = [entry("a", 30, 50)]
doomed = plan_deletions(viejo_y_gordo, max_age_days=14,
                        max_total_bytes=1 * 1024 ** 2, now=NOW)
check("un clip que cae por edad y por tamaño se cuenta una sola vez",
      [e.clip_id for e in doomed] == ["a"], f"({[e.clip_id for e in doomed]})")


# ---------------------------------------------------------------------------
print("\n-- resolve: la barrera contra el path traversal --")

store, root = make_store()
clip = make_clip(store, "huerta")
cid = store.clip_id_of(clip)

check("un clip normal se resuelve", store.resolve(cid) == clip.resolve())

for malo, why in [
    ("../../main.py", "subir con .."),
    ("huerta/../../../etc/passwd", ".. enterrado"),
    ("/etc/passwd", "ruta absoluta"),
    ("\\windows\\system32\\drivers\\etc\\hosts", "ruta absoluta de Windows"),
    ("", "vacío"),
    (cid.replace(".mp4", ".json"), "el sidecar no se sirve por aquí"),
    (part_path(clip).name, "un .part no es un clip terminado"),
]:
    try:
        store.resolve(malo)
        ok = False
    except ValueError:
        ok = True
    check(f"resolve rechaza {why}", ok, f"({malo!r})")

try:
    store.resolve("huerta/2000-01-01/no-existe.mp4")
    ok = False
except ValueError:
    ok = True
check("resolve rechaza un clip que no existe", ok)

# camera_id lo elige quien registra la cámara por API: si no se saneara, un id
# con .. escribiría fuera de la carpeta de clips.
travieso = store.new_clip_path("../../pwned", "detection")
check("un camera_id con .. no se sale de la raíz",
      travieso.resolve().is_relative_to(root.resolve()), f"({travieso})")


# ---------------------------------------------------------------------------
print("\n-- listado --")

store, root = make_store()
make_clip(store, "huerta", ts=NOW - 3 * DAY)
make_clip(store, "huerta", ts=NOW - 1 * DAY)
make_clip(store, "porche", ts=NOW - 2 * DAY)
part = part_path(store.new_clip_path("huerta", "detection"))
part.write_bytes(b"\x00" * 1024)

todos = store.list_clips()
check("lista todos los clips de todas las cámaras", len(todos) == 3, f"({len(todos)})")
check("el .part NO aparece en el listado",
      not any(PART_SUFFIX in e.clip_id for e in todos))
check("ordena de más reciente a más antiguo",
      [e.mtime for e in todos] == sorted((e.mtime for e in todos), reverse=True))
check("filtra por cámara", len(store.list_clips("huerta")) == 2)
check("una cámara sin clips devuelve lista vacía", store.list_clips("nadie") == [])
check("lee el sidecar al listar", todos[0].meta.get("camera_id") in ("huerta", "porche"))

st = store.stats()
check("stats cuenta los clips", st["clips"] == 3, f"({st['clips']})")
check("stats desglosa por cámara",
      st["per_camera"]["huerta"]["clips"] == 2 and st["per_camera"]["porche"]["clips"] == 1)
check("stats informa del hueco libre", st["free_gb"] > 0)


# ---------------------------------------------------------------------------
print("\n-- borrado y barrido --")

store, root = make_store(max_age_days=14, max_total_gb=0)
viejo = make_clip(store, "huerta", ts=NOW - 30 * DAY)
nuevo = make_clip(store, "huerta", ts=time.time())
ajeno = root / "no-tocar.txt"
ajeno.write_text("no soy un clip")

res = store.sweep(dry_run=True)
check("dry_run informa de lo que borraría", len(res["deleted"]) == 1, f"({res['deleted']})")
check("dry_run NO borra nada", viejo.is_file())

res = store.sweep()
check("el barrido borra el clip caducado", not viejo.is_file())
check("y también su sidecar y su miniatura",
      not viejo.with_suffix(".json").is_file() and not viejo.with_suffix(".jpg").is_file())
check("deja en paz el clip reciente", nuevo.is_file())
check("no toca ficheros que no son clips", ajeno.is_file())
check("informa de los bytes liberados", res["freed_bytes"] > 0, f"({res['freed_bytes']})")

store.mark_in_progress(store.clip_id_of(nuevo))
store.cfg.max_age_days = 0.0000001  # todo es "viejo"
res = store.sweep()
check("el barrido respeta el clip en curso", nuevo.is_file(), f"({res['deleted']})")
store.unmark_in_progress(store.clip_id_of(nuevo))

# Guarda contra una configuración desastrosa: root_dir apuntando a la raíz de
# una unidad convertiría el barrido en un formateo lento.
store_malo = ClipStore(RecordingsConfig(root_dir=str(Path(root.resolve().anchor))))
res = store_malo.sweep()
check("no barre si root_dir es la raíz de una unidad",
      res["deleted"] == [] and "error" in res, f"({res})")

store_fantasma = ClipStore(RecordingsConfig(root_dir=str(root / "no-existe")))
check("no barre si root_dir no existe", store_fantasma.sweep()["deleted"] == [])
check("y listar tampoco revienta", store_fantasma.list_clips() == [])


# ---------------------------------------------------------------------------
print("\n-- recuperación de .part huérfanos --")

store, root = make_store()
con_datos = part_path(store.new_clip_path("huerta", "detection"))
con_datos.write_bytes(b"\x00" * 4096)
write_sidecar(con_datos.with_name(con_datos.name[:-len(PART_SUFFIX)] + ".mp4"),
              {"camera_id": "huerta"})
time.sleep(0.01)
vacio = part_path(store.new_clip_path("huerta", "detection"))
vacio.touch()

recuperados = store.recover_orphan_parts()
check("rescata el .part con contenido", len(recuperados) == 1, f"({recuperados})")
check("marcándolo como truncado en el nombre", "truncado" in recuperados[0])
check("borra el .part vacío", not vacio.is_file())
check("no quedan .part sueltos",
      not any(p.name.endswith(PART_SUFFIX) for p in root.rglob("*")))

rescatado = store.resolve(recuperados[0])
check("el clip rescatado se puede resolver y descargar", rescatado.is_file())
meta = json.loads(rescatado.with_suffix(".json").read_text(encoding="utf-8"))
check("y su sidecar avisa de que está truncado", meta.get("truncated") is True, f"({meta})")


# ---------------------------------------------------------------------------
print("\n-- persistencia de la configuración --")

import clips as clips_mod  # noqa: E402

tmpcfg = Path(tempfile.mkdtemp(prefix="cliptest-cfg-")) / "recordings_config.json"
orig, clips_mod.RECORDINGS_CONFIG_FILE = clips_mod.RECORDINGS_CONFIG_FILE, tmpcfg

check("sin fichero devuelve los defectos",
      clips_mod.load_recordings_config().max_age_days == RecordingsConfig().max_age_days)

clips_mod.save_recordings_config(RecordingsConfig(
    root_dir="D:/clips", max_age_days=3, max_total_gb=5, min_free_gb=1))
back = clips_mod.load_recordings_config()
check("la config sobrevive a la ida y vuelta",
      (back.root_dir, back.max_age_days, back.max_total_gb) == ("D:/clips", 3.0, 5.0),
      f"({back.dict()})")
check("max_total_bytes traduce los GB", back.max_total_bytes == 5 * 1024 ** 3)

tmpcfg.write_text("{esto no es json", encoding="utf-8")
check("un JSON corrupto no impide arrancar",
      clips_mod.load_recordings_config().root_dir == RecordingsConfig().root_dir)

clips_mod.RECORDINGS_CONFIG_FILE = orig
shutil.rmtree(tmpcfg.parent, ignore_errors=True)


print("\n" + ("TODO OK" if not failures else f"FALLOS: {failures}"))
sys.exit(1 if failures else 0)
