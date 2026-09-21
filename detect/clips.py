"""Los clips de vídeo que hay en disco: dónde van, cómo se listan y cuándo se borran.

Este módulo es la mitad "global" de la grabación. La otra mitad, `recorder.py`,
es lo que pasa frame a frame dentro de UNA cámara; aquí no se sabe nada de
detecciones ni de sesiones, solo de ficheros.

Se separan porque el ciclo de vida es distinto: un `ClipRecorder` nace y muere
con su cámara, mientras que el tope de disco es del disco, no de la cámara, y su
barrido tiene que correr aunque no haya ninguna sesión viva. Es la misma frontera
que separa `servo_tracker.py` (por cámara) de `registry.py` (global).

Cada clip son hasta tres ficheros con el mismo nombre base:

    huerta/2026-09-20/huerta_20260920-181233_det.mp4    el vídeo
    huerta/2026-09-20/huerta_20260920-181233_det.json   los metadatos (sidecar)
    huerta/2026-09-20/huerta_20260920-181233_det.jpg    la miniatura

El sidecar existe para que listar no tenga que abrir ni un solo MP4: la lista de
`GET /recordings` sale de leer JSON pequeños, no de demuxar vídeo. Y mientras se
graba el fichero se llama `....part.mp4`, así que nunca aparece en el listado,
nunca lo borra la retención y nadie se descarga un MP4 sin `moov`.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from pydantic import BaseModel, Field

from log import print


# Dónde se guardan los ajustes globales de grabación. Aparte de
# cameras_config.json porque ese fichero es una LISTA de cámaras; esto es un
# objeto único y de ámbito distinto.
RECORDINGS_CONFIG_FILE = Path(__file__).with_name("recordings_config.json")

# Sufijo del fichero mientras se está escribiendo. El punto va dentro para que
# Path.suffix siga siendo ".mp4" y el fichero se reconozca como vídeo a medias
# en vez de como un formato desconocido.
PART_SUFFIX = ".part.mp4"

# Marca que se le pone a un .part rescatado de un apagado sucio. Sale en el
# listado (es vídeo de verdad) pero avisa de que puede estar truncado.
TRUNCATED_MARK = "-truncado"


class RecordingsConfig(BaseModel):
    """Ajustes globales de almacenamiento, persistidos en recordings_config.json.

    No viven en `GLOBAL_CONFIG` de camera.py, que es el sitio "natural", porque
    ese diccionario NO se persiste: se pierde en cada /service/restart. Una
    retención que se olvida al reiniciar no es una retención, es una bomba de
    relojería en el disco donde además corre YOLO.
    """

    # Raíz de los clips. Relativa al módulo si no es absoluta, para que el
    # defecto funcione sin configurar nada.
    root_dir: str = "clips"
    # Los clips más viejos que esto se borran. 0 = sin límite por edad.
    max_age_days: float = 14.0
    # Tope de ocupación de la carpeta entera. 0 = sin límite por tamaño.
    max_total_gb: float = 20.0
    # Cada cuánto corre el barrido. Además se barre al arrancar y tras cerrar
    # cada clip, así que este intervalo solo cubre el caso "el servicio lleva
    # días encendido sin grabar nada y algo caducó".
    sweep_interval_sec: float = 600.0
    # Por debajo de este hueco libre en disco NO se abren clips nuevos. La
    # retención borra lo viejo, pero si el disco se llena por otra cosa (los
    # pesos de los modelos, otro programa) más vale dejar de grabar que tumbar
    # el servicio entero.
    min_free_gb: float = 2.0

    @property
    def max_total_bytes(self) -> int:
        return int(self.max_total_gb * 1024 ** 3)

    @property
    def min_free_bytes(self) -> int:
        return int(self.min_free_gb * 1024 ** 3)


@dataclass
class ClipEntry:
    """Un clip ya cerrado, tal y como está en disco.

    `clip_id` es la ruta relativa a la raíz con barras normales, y es lo que
    viaja por la API: sirve de identificador y de ruta a la vez, así que no hace
    falta un índice ni una base de datos.
    """

    clip_id: str
    path: Path
    size: int
    mtime: float
    meta: dict = field(default_factory=dict)

    @property
    def sidecar(self) -> Path:
        return self.path.with_suffix(".json")

    @property
    def thumbnail(self) -> Path:
        return self.path.with_suffix(".jpg")


# ---------------------------------------------------------------------------
# Persistencia de la configuración global
# ---------------------------------------------------------------------------

def load_recordings_config() -> RecordingsConfig:
    if not RECORDINGS_CONFIG_FILE.exists():
        return RecordingsConfig()
    try:
        data = json.loads(RECORDINGS_CONFIG_FILE.read_text(encoding="utf-8"))
        return RecordingsConfig(**data)
    except Exception as e:
        # Un JSON corrupto no puede impedir que arranque el servicio: se avisa
        # y se sigue con los defectos, que son conservadores.
        print(f"No se pudo leer {RECORDINGS_CONFIG_FILE}: {e!r}; se usan los defectos")
        return RecordingsConfig()


def save_recordings_config(cfg: RecordingsConfig) -> None:
    try:
        RECORDINGS_CONFIG_FILE.write_text(
            json.dumps(cfg.dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        print(f"No se pudo guardar {RECORDINGS_CONFIG_FILE}: {e!r}")


# ---------------------------------------------------------------------------
# Decisión de qué borrar: función pura, sin disco, para poder probarla
# ---------------------------------------------------------------------------

def plan_deletions(
    entries: Iterable[ClipEntry],
    max_age_days: float,
    max_total_bytes: int,
    keep: Optional[set[str]] = None,
    now: Optional[float] = None,
) -> list[ClipEntry]:
    """Qué clips hay que borrar, en orden de borrado.

    Primero caduca por edad, y solo DESPUÉS se mira el tamaño sobre lo que
    queda: al revés se borrarían clips recientes para hacer hueco a otros que
    iban a caducar en la misma pasada.

    `keep` son los clip_id intocables (el clip que se está grabando ahora
    mismo). Un tope a 0 significa "sin límite", no "bórralo todo".
    """
    now = time.time() if now is None else now
    keep = keep or set()

    alive = sorted((e for e in entries if e.clip_id not in keep), key=lambda e: e.mtime)
    doomed: list[ClipEntry] = []

    if max_age_days > 0:
        cutoff = now - max_age_days * 86400
        still = []
        for e in alive:
            (doomed if e.mtime < cutoff else still).append(e)
        alive = still

    if max_total_bytes > 0:
        total = sum(e.size for e in alive)
        # Los más antiguos primero: `alive` ya está ordenado por mtime
        # ascendente, así que se va consumiendo por la cabeza.
        for e in alive:
            if total <= max_total_bytes:
                break
            doomed.append(e)
            total -= e.size

    return doomed


# ---------------------------------------------------------------------------
# El almacén
# ---------------------------------------------------------------------------

class ClipStore:
    """La carpeta de clips: crea rutas, lista, borra y aplica la retención.

    Una instancia por servicio (`STORE` al final del módulo). El `ClipRecorder`
    la recibe inyectada, así que en los tests se le puede pasar un store que
    apunte a un directorio temporal sin tocar nada del sistema.
    """

    def __init__(self, cfg: Optional[RecordingsConfig] = None):
        self._lock = threading.Lock()
        self.cfg = cfg or RecordingsConfig()
        # clip_id de los clips que se están grabando ahora mismo. La retención
        # los respeta aunque el .part ya pese lo suyo.
        self._in_progress: set[str] = set()

    # -- rutas ---------------------------------------------------------------

    @property
    def root(self) -> Path:
        p = Path(self.cfg.root_dir)
        if not p.is_absolute():
            p = Path(__file__).parent / p
        return p

    def new_clip_path(self, camera_id: str, trigger: str, ts: Optional[float] = None) -> Path:
        """Reserva la ruta DEFINITIVA de un clip nuevo (el .part se deriva de ella).

        El nombre lleva la fecha en formato ordenable y el disparo (`det`/`man`)
        para que la carpeta se pueda leer sin abrir nada. Hora local, la misma
        que el log del servicio, para poder cotejar clip y log a ojo.

        Crea la carpeta del día: quien llama está a punto de abrir el encoder
        ahí, y que falle por un directorio inexistente sería un error tonto en
        un sitio (el hilo escritor) donde cuesta diagnosticarlo.
        """
        ts = time.time() if ts is None else ts
        lt = time.localtime(ts)
        day = time.strftime("%Y-%m-%d", lt)
        stamp = time.strftime("%Y%m%d-%H%M%S", lt)
        suffix = "man" if trigger == "manual" else "det"
        safe_cam = _safe_name(camera_id)
        base = self.root / safe_cam / day
        path = base / f"{safe_cam}_{stamp}_{suffix}.mp4"
        # Dos eventos en el mismo segundo en la misma cámara: se desempata con
        # un contador en vez de pisar el clip anterior.
        n = 1
        while path.exists() or part_path(path).exists():
            path = base / f"{safe_cam}_{stamp}-{n}_{suffix}.mp4"
            n += 1
        base.mkdir(parents=True, exist_ok=True)
        return path

    def clip_id_of(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def resolve(self, clip_id: str) -> Path:
        """clip_id (que viene de fuera) → ruta real, o ValueError.

        Es la única puerta de entrada a un fichero desde la API, así que aquí
        se para el path traversal: sin esto, `GET /recordings/../../main.py` se
        descargaría el código fuente.
        """
        if not clip_id or clip_id.startswith(("/", "\\")) or "\0" in clip_id:
            raise ValueError("clip_id inválido")
        root = self.root.resolve()
        p = (root / clip_id).resolve()
        if not p.is_relative_to(root):
            raise ValueError("clip_id fuera de la carpeta de clips")
        if p.suffix.lower() != ".mp4" or p.name.endswith(PART_SUFFIX):
            raise ValueError("clip_id no es un clip terminado")
        if not p.is_file():
            raise ValueError("clip no encontrado")
        return p

    # -- clips en curso ------------------------------------------------------

    def mark_in_progress(self, clip_id: str) -> None:
        with self._lock:
            self._in_progress.add(clip_id)

    def unmark_in_progress(self, clip_id: str) -> None:
        with self._lock:
            self._in_progress.discard(clip_id)

    def in_progress(self) -> set[str]:
        with self._lock:
            return set(self._in_progress)

    # -- listado -------------------------------------------------------------

    def list_clips(self, camera_id: Optional[str] = None) -> list[ClipEntry]:
        """Todos los clips terminados, más recientes primero.

        Recorre el árbol con os.walk en vez de rglob porque hay que saltarse
        los .part y leer el sidecar al vuelo, y así se hace una sola pasada.
        """
        root = self.root
        if not root.is_dir():
            return []
        base = root / _safe_name(camera_id) if camera_id else root
        if not base.is_dir():
            return []

        out: list[ClipEntry] = []
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if not name.lower().endswith(".mp4") or name.endswith(PART_SUFFIX):
                    continue
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:
                    continue  # borrado entre el walk y el stat
                out.append(ClipEntry(
                    clip_id=self.clip_id_of(p),
                    path=p,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    meta=_read_sidecar(p),
                ))
        out.sort(key=lambda e: e.mtime, reverse=True)
        return out

    def stats(self) -> dict:
        clips = self.list_clips()
        per_camera: dict[str, dict] = {}
        for e in clips:
            cam = e.clip_id.split("/", 1)[0]
            slot = per_camera.setdefault(cam, {"clips": 0, "bytes": 0})
            slot["clips"] += 1
            slot["bytes"] += e.size
        total = sum(e.size for e in clips)
        return {
            "root_dir": str(self.root),
            "clips": len(clips),
            "total_bytes": total,
            "total_gb": round(total / 1024 ** 3, 3),
            "oldest": clips[-1].clip_id if clips else None,
            "newest": clips[0].clip_id if clips else None,
            "free_gb": round(self.free_bytes() / 1024 ** 3, 2),
            "in_progress": sorted(self.in_progress()),
            "per_camera": per_camera,
        }

    # -- disco ---------------------------------------------------------------

    def free_bytes(self) -> int:
        try:
            # La raíz puede no existir todavía: se pregunta por el ancestro más
            # cercano que sí exista, que está en el mismo volumen.
            p = self.root
            while not p.exists() and p != p.parent:
                p = p.parent
            return shutil.disk_usage(p).free
        except OSError:
            return 0

    def has_room(self) -> bool:
        return self.free_bytes() >= self.cfg.min_free_bytes

    # -- borrado -------------------------------------------------------------

    def delete(self, entry: ClipEntry) -> int:
        """Borra el clip y sus acompañantes. Devuelve los bytes liberados.

        Los tres ficheros se borran juntos a propósito: un sidecar huérfano
        haría que el listado prometiera un vídeo que ya no está.
        """
        freed = 0
        for p in (entry.path, entry.sidecar, entry.thumbnail):
            try:
                if p.is_file():
                    freed += p.stat().st_size
                    p.unlink()
            except OSError as e:
                print(f"No se pudo borrar {p}: {e!r}")
        _prune_empty_dirs(entry.path.parent, self.root)
        return freed

    def sweep(self, dry_run: bool = False) -> dict:
        """Aplica la retención. Devuelve qué se borró (o se borraría) y por qué."""
        root = self.root
        # Guarda tonta pero importante: si root_dir se configura mal y acaba
        # siendo la raíz de una unidad, este método NO puede ponerse a borrar.
        if not root.is_dir() or root.resolve() == Path(root.resolve().anchor):
            return {"deleted": [], "freed_bytes": 0, "dry_run": dry_run,
                    "error": f"root_dir no válido: {root}"}

        entries = self.list_clips()
        doomed = plan_deletions(
            entries,
            max_age_days=self.cfg.max_age_days,
            max_total_bytes=self.cfg.max_total_bytes,
            keep=self.in_progress(),
        )
        freed = 0
        deleted: list[str] = []
        for e in doomed:
            if not dry_run:
                freed += self.delete(e)
            else:
                freed += e.size
            deleted.append(e.clip_id)
        if deleted and not dry_run:
            print(f"retención: {len(deleted)} clip(s) borrados, "
                  f"{freed / 1024 ** 2:.1f} MB liberados")
        return {"deleted": deleted, "freed_bytes": freed, "dry_run": dry_run,
                "remaining": len(entries) - len(deleted)}

    def recover_orphan_parts(self) -> list[str]:
        """Rescata los .part que dejó un apagado sucio.

        Un .part con contenido es vídeo de verdad al que solo le falta el cierre
        del contenedor: se conserva marcado como truncado en vez de tirarlo, que
        es justo el clip del incidente que tumbó el servicio. Uno vacío no sirve
        para nada.
        """
        root = self.root
        if not root.is_dir():
            return []
        recovered: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(PART_SUFFIX):
                    continue
                p = Path(dirpath) / name
                try:
                    if p.stat().st_size == 0:
                        p.unlink()
                        continue
                    final = p.with_name(name[:-len(PART_SUFFIX)] + TRUNCATED_MARK + ".mp4")
                    os.replace(p, final)
                except OSError as e:
                    print(f"No se pudo recuperar {p}: {e!r}")
                    continue
                _mark_truncated_sidecar(final)
                recovered.append(self.clip_id_of(final))
        if recovered:
            print(f"grabación: {len(recovered)} clip(s) truncados recuperados del arranque anterior")
        return recovered


# ---------------------------------------------------------------------------
# El hilo de retención
# ---------------------------------------------------------------------------

class RetentionSweeper:
    """Corre `ClipStore.sweep()` cada cierto tiempo, en un hilo aparte.

    Uno global, no uno por cámara: el tope en GB es del disco. Duerme sobre un
    Event en vez de sobre time.sleep para que el apagado no tenga que esperar
    hasta el próximo barrido.
    """

    def __init__(self, store: ClipStore):
        self._store = store
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_sweep: Optional[dict] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="clip-retention", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # Barrido inmediato al arrancar: recoge lo que caducó mientras el
        # servicio estaba apagado, que es cuando más fácil es pasarse del tope.
        while not self._stop.is_set():
            try:
                self.last_sweep = self._store.sweep()
            except Exception as e:
                print(f"retención: fallo en el barrido: {e!r}")
            self._stop.wait(max(30.0, self._store.cfg.sweep_interval_sec))

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------

def part_path(final: Path) -> Path:
    """Ruta temporal (.part.mp4) que corresponde a un clip definitivo."""
    return final.with_name(final.stem + PART_SUFFIX)


def _safe_name(name: str) -> str:
    """camera_id → nombre de carpeta seguro.

    El camera_id lo elige quien registra la cámara por API, así que puede traer
    barras o puntos: sin sanear, un id como `../..` escribiría fuera de la raíz.
    """
    keep = "-_."
    out = "".join(c if (c.isalnum() or c in keep) else "_" for c in (name or "sin_id"))
    out = out.strip(". ")
    return out or "sin_id"


def _read_sidecar(clip: Path) -> dict:
    try:
        return json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
    except Exception:
        # Sin sidecar el clip sigue siendo válido y descargable: es lo que pasa
        # con los rescatados de un apagado sucio.
        return {}


def write_sidecar(clip: Path, meta: dict) -> None:
    try:
        clip.with_suffix(".json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        print(f"No se pudo escribir el sidecar de {clip.name}: {e!r}")


def _mark_truncated_sidecar(final: Path) -> None:
    """El sidecar del .part rescatado (si lo hay) pasa a llamarse como el mp4."""
    old = final.with_name(final.name[:-len(TRUNCATED_MARK + ".mp4")] + ".json")
    meta = {}
    try:
        if old.is_file():
            meta = json.loads(old.read_text(encoding="utf-8"))
            old.unlink()
    except Exception:
        meta = {}
    meta.update({"truncated": True, "clip_id": final.name})
    write_sidecar(final, meta)


def _prune_empty_dirs(start: Path, root: Path) -> None:
    """Quita las carpetas de día que quedan vacías tras borrar su último clip."""
    try:
        root = root.resolve()
        p = start.resolve()
    except OSError:
        return
    while p != root and p.is_relative_to(root):
        try:
            next(p.iterdir())
            return  # no está vacía
        except StopIteration:
            try:
                p.rmdir()
            except OSError:
                return
        except OSError:
            return
        p = p.parent


# Instancia global del servicio. Los tests construyen la suya con un root
# temporal y no tocan esta.
STORE = ClipStore()
