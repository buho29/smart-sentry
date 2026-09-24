"""Graba clips de una cámara: decide cuándo, y escribe sin frenar la inferencia.

Es el segundo consumidor de detecciones del proyecto, después de
`servo_tracker.py`. La parte interesante no es codificar vídeo —de eso se
encarga `encoders.py`— sino **no molestar**: todos los métodos del
`DetectionConsumer` corren en el hilo `yolo-<camera_id>`, el mismo que hace la
inferencia y alimenta a quien esté mirando el stream. Un `VideoWriter.write()` o
un `subprocess` ahí dentro le metería una pausa al pipeline entero cada vez que
el disco tuviera un mal día.

Así que lo que hace este módulo en el hilo de la cámara es exclusivamente O(1):
apuntar el JPEG en el pre-roll y encolarlo. Un hilo escritor por cámara saca de
la cola y es el único que habla con el encoder y con el disco.

De dónde salen los frames: de los JPEG que la sesión ya ha codificado, no del
ndarray. A 640x480 un frame en crudo son ~0,9 MB y en JPEG ~45 KB, así que un
pre-roll de cinco segundos pasa de 69 MB a 3,4 MB por cámara. Además, con ffmpeg
esos JPEG se le pasan tal cual por la tubería: no se decodifica ni un frame en
todo el camino.

El pre-roll es el motivo de existir de todo esto. Un clip que empieza cuando
YOLO ya ha reconocido al gato empieza con el gato a medio salir del encuadre;
guardando los últimos segundos en memoria, el clip empieza antes de que el gato
apareciera.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

from clips import ClipStore, part_path, write_sidecar
from detections import Detection
from encoders import VideoEncoder, frames_for_gap, jpeg_size, resolve_encoder
from log import print


class RecordingConfig(BaseModel):
    """Ajustes de grabación de UNA cámara, persistidos en cameras_config.json.

    Una cámara que no graba simplemente no trae esta sección (`recording:
    None`), igual que pasa con `servo`, y entonces no se crea el consumidor ni
    se codifica un JPEG de más.

    Solo lleva lo que tiene sentido distinto en cada cámara. Cómo se codifica
    el vídeo y los topes de memoria son de la máquina y viven en
    `RecordingsConfig` (clips.py). Qué clases y con qué confianza disparan
    tampoco está aquí: el grabador recibe las detecciones ya filtradas por
    `classes` y `confidence` de la propia cámara.
    """

    enabled: bool = True

    # -- qué se graba --------------------------------------------------------
    # "annotated" mete en el vídeo las cajas y el texto de YOLO, que es lo que
    # se quiere para revisar una detección. Cuidado: pedir el anotado ENCIENDE
    # la inferencia en esa cámara aunque no haya nadie mirando el stream, con
    # su coste de GPU. "raw" graba la imagen limpia y no cuesta GPU ninguna.
    source: str = "annotated"

    # -- disparo por detección ----------------------------------------------
    trigger_on_detection: bool = True
    # Frames consecutivos con detección antes de abrir un clip. A 1, un falso
    # positivo suelto —una hoja movida que YOLO ve como un pájaro— genera un
    # clip; a 2 hay que fallar dos veces seguidas en el mismo sitio.
    min_hits: int = 2

    # -- duración ------------------------------------------------------------
    # Segundos ANTERIORES al disparo que se incluyen. Se guardan en memoria:
    # 5 s a 15 fps son unos 3,4 MB a 640x480.
    pre_roll_sec: float = 5.0
    # Segundos sin detecciones tras los cuales se cierra el clip. Conviene que
    # sea generoso: si es más corto que las pausas del bicho de turno, un solo
    # evento sale partido en diez clips.
    post_roll_sec: float = 8.0
    # Corte duro. Si el evento sigue, se cierra este clip y se abre otro, para
    # que una cámara enfocando una carretera no genere un fichero de 8 GB.
    max_clip_sec: float = 120.0
    # Los clips más cortos que esto se tiran al cerrarlos: casi siempre son
    # falsos positivos y solo sirven para llenar el listado.
    min_clip_sec: float = 2.0
    # Tiempo muerto tras cerrar antes de poder disparar otra vez.
    cooldown_sec: float = 3.0

    # -- fichero -------------------------------------------------------------
    # FPS del MP4. None = se mide del pipeline al abrir cada clip.
    fps: Optional[float] = None


# Tipos de mensaje del hilo escritor. El estado (abrir/cerrar) viaja por la
# MISMA cola que los frames: si fueran por caminos distintos, un "cierra" podría
# adelantar a frames que todavía estaban en vuelo y cortar el clip antes de
# tiempo.
_OPEN, _FRAME, _CLOSE, _QUIT = "open", "frame", "close", "quit"


@dataclass
class _OpenMsg:
    final: Path
    part: Path
    width: int
    height: int
    fps: float
    meta: dict


@dataclass
class _CloseMsg:
    meta: dict
    discard: bool = False
    # Cuánto se le deja al encoder para terminar. Viaja en el mensaje y no en
    # un atributo del grabador porque lo fija el hilo de la cámara y lo lee el
    # escritor: por aquí no hay carrera posible.
    timeout: float = 5.0


class ClipRecorder:
    """Decide cuándo grabar y encola los frames. No escribe nada él mismo.

    `store` y `encoder_factory` se inyectan para poder probar toda la máquina de
    estados con un encoder de mentira y un directorio temporal, sin ffmpeg, sin
    GPU y sin placa — el mismo truco que usa `ServoTracker` con `esphome`.
    """

    def __init__(self, cfg: RecordingConfig, camera_id: str, store: ClipStore,
                 fps_getter: Callable[[], float] = lambda: 0.0,
                 encoder_factory: Optional[Callable[[], tuple[VideoEncoder, str]]] = None):
        self.cfg = cfg
        self._camera_id = camera_id
        self._store = store
        self._fps_getter = fps_getter
        self._encoder_factory = encoder_factory

        self._lock = threading.RLock()

        # -- estado de la máquina (siempre bajo _lock) -----------------------
        self._state = "idle"              # "idle" | "recording"
        self._trigger: Optional[str] = None
        self._clip_final: Optional[Path] = None
        self._clip_id: Optional[str] = None
        self._started_at = 0.0
        self._frames = 0
        self._dropped = 0
        self._last_det_ts = 0.0
        self._cooldown_until = 0.0
        self._hits = 0
        self._labels: dict[str, int] = {}
        self._max_conf = 0.0
        self._note: Optional[str] = None
        self._size: Optional[tuple[int, int]] = None

        # Disparo pendiente de aplicar. Se marca en on_detections (o en
        # start_manual) y se materializa en el siguiente on_jpeg, que es quien
        # trae los bytes y las dimensiones.
        self._pending_open: Optional[str] = None

        # -- pre-roll ---------------------------------------------------------
        self._pre: deque[tuple[bytes, float]] = deque()
        self._pre_bytes = 0

        # -- hilo escritor ----------------------------------------------------
        self._q: queue.Queue = queue.Queue(maxsize=max(8, store.cfg.queue_maxsize))
        self._thread: Optional[threading.Thread] = None
        self._opened_evt = threading.Event()
        self._closed_evt = threading.Event()
        self.last_clip: Optional[dict] = None
        self.last_error: Optional[str] = None
        self.encoder_name: Optional[str] = None
        self.clips_written = 0

    # -- interfaz DetectionConsumer -----------------------------------------

    def on_detections(self, dets: list[Detection], width: int, height: int) -> None:
        """Decide si este frame dispara o mantiene la grabación. No escribe.

        Corre ANTES de que llegue el JPEG de este mismo frame (ver el orden en
        `_process_loop`), y por eso el frame que dispara acaba dentro del clip
        en vez de ser el primero que se pierde.
        """
        if not self.cfg.enabled or not self.cfg.trigger_on_detection:
            return

        # Cualquier detección vale: la sesión ya las ha filtrado por `classes`
        # y `confidence` de la cámara (y ha quitado las corruptas).
        if not dets:
            self._hits = 0
            return

        now = time.time()
        with self._lock:
            self._hits += 1
            if self._state == "recording":
                self._last_det_ts = now
                self._tally(dets)
            elif (self._hits >= self.cfg.min_hits
                    and self._pending_open is None
                    and now >= self._cooldown_until):
                self._pending_open = "detection"
                self._last_det_ts = now
                self._tally(dets)

    def on_jpeg(self, raw: Optional[bytes], annotated: Optional[bytes], ts: float) -> None:
        """Un frame nuevo ya codificado. TODO lo de aquí es O(1).

        Ni disco, ni encoder, ni red: apuntar en el pre-roll, encolar y volver.
        """
        if not self.cfg.enabled:
            return

        jpg = annotated if self.cfg.source == "annotated" else raw
        if jpg is None:
            # El anotado puede faltar en un frame suelto (la inferencia falló, o
            # todavía no ha arrancado). Mejor grabar la imagen limpia que dejar
            # un hueco en el vídeo.
            jpg = raw if annotated is None else annotated
        if jpg is None:
            return

        with self._lock:
            if self._state == "recording":
                self._push(jpg, ts)
                self._check_close(ts, jpg)
            else:
                self._remember(jpg, ts)
                if self._pending_open is not None:
                    self._open(self._pending_open, jpg, ts)

    def on_idle(self) -> None:
        """No llegan frames. Hay que poder cerrar igual.

        Si la placa se duerme o pierde el wifi a media grabación, sin esto el
        clip se quedaría abierto hasta el próximo evento y saldría un fichero
        con dos sucesos de horas distintas pegados.
        """
        if not self.cfg.enabled:
            return
        with self._lock:
            if self._state != "recording" or self._trigger == "manual":
                return
            if time.time() - self._last_det_ts >= self.cfg.post_roll_sec:
                self._close("sin frames")

    def wants_inference(self) -> bool:
        # Grabar por detección obliga a inferir aunque no haya nadie mirando el
        # stream; si no, la grabación automática se moriría al cerrar el
        # navegador. Igual que el seguimiento de servos.
        return self.cfg.enabled and self.cfg.trigger_on_detection

    def wants_frames(self) -> Optional[str]:
        """Qué JPEG necesita la sesión codificar para este grabador.

        Devuelve algo también cuando NO se está grabando: el pre-roll necesita
        frames continuos, o el clip empezaría en el instante del disparo y se
        perdería justo lo interesante. A `enabled=False` devuelve None y el
        coste vuelve a ser exactamente cero.
        """
        if not self.cfg.enabled:
            return None
        return "annotated" if self.cfg.source == "annotated" else "raw"

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            # `enabled` y `source` ya salen en config.recording, y el disco
            # libre es de la máquina: está en /recordings/stats.
            return {
                "state": self._state,
                "trigger": self._trigger,
                "encoder": self.encoder_name,
                "current_clip": self._clip_id,
                "elapsed_sec": round(now - self._started_at, 1) if self._state == "recording" else 0.0,
                "frames": self._frames,
                # Si esto sube, el disco no da abasto y el clip tendrá saltos.
                "dropped_frames": self._dropped,
                "queue": self._q.qsize(),
                "preroll_frames": len(self._pre),
                "preroll_mb": round(self._pre_bytes / 1024 ** 2, 2),
                "clips_written": self.clips_written,
                "last_clip": self.last_clip,
                "last_error": self.last_error,
            }

    def shutdown(self) -> None:
        """Cierra el clip en curso con el presupuesto de apagado que hay.

        Los consumidores se avisan antes de parar los hilos de vídeo, y el
        margen total es de unos pocos segundos, así que aquí no se puede
        esperar indefinidamente a ffmpeg. Si no da tiempo, el fichero se queda
        como .part y el próximo arranque lo rescata: es mejor un clip truncado
        que un apagado que se cuelga.
        """
        with self._lock:
            if self._state == "recording":
                self._close("apagado", timeout=1.5)
        self._stop_thread(timeout=2.0)

    # -- disparo manual (desde la API, otro hilo) ---------------------------

    def start_manual(self, note: Optional[str] = None, timeout: float = 3.0) -> dict:
        """Arranca un clip manual y espera a que abra de verdad.

        Espera en vez de devolver "armado" porque quien llama a esto desde
        Swagger o desde un rest_command quiere saber si está grabando, no si lo
        intentará cuando llegue un frame. El clip incluye el pre-roll que
        hubiera, así que un start manual tras oír algo ya trae los segundos
        anteriores.
        """
        if not self.cfg.enabled:
            raise RuntimeError("la grabación está deshabilitada en esta cámara")
        if not self._store.has_room():
            raise OSError(f"queda menos de {self._store.cfg.min_free_gb} GB libres")
        with self._lock:
            if self._state == "recording" and self._trigger == "manual":
                raise RuntimeError(f"ya hay una grabación manual en curso: {self._clip_id}")
            self._note = note
            if self._state == "recording":
                # Se adopta el clip que había abierto por detección: partirlo
                # en dos para empezar uno "manual" perdería justo el trozo por
                # el que el usuario ha pulsado.
                self._trigger = "manual"
                return self.status()
            self._opened_evt.clear()
            self._pending_open = "manual"
            self._cooldown_until = 0.0  # una orden manual no espera al cooldown

        if not self._opened_evt.wait(timeout):
            with self._lock:
                self._pending_open = None
            raise TimeoutError(
                "no ha llegado ningún frame: ¿está la cámara arrancada y dando imagen?")
        return self.status()

    def stop_manual(self, timeout: float = 6.0) -> dict:
        """Cierra el clip y espera al fichero para poder devolver sus metadatos."""
        with self._lock:
            if self._state != "recording":
                raise RuntimeError("no hay ninguna grabación en curso")
            self._closed_evt.clear()
            self._close("parada manual")
        self._closed_evt.wait(timeout)
        return {"clip": self.last_clip, "recorder": self.status()}

    # -- máquina de estados (siempre bajo _lock) ----------------------------

    def _tally(self, dets: list[Detection]) -> None:
        """Apunta qué se ha visto, para el sidecar del clip."""
        for d in dets:
            self._labels[d.label] = self._labels.get(d.label, 0) + 1
            self._max_conf = max(self._max_conf, d.conf)

    def _remember(self, jpg: bytes, ts: float) -> None:
        """Mete el frame en el pre-roll y poda lo que sobra.

        Se poda por segundos Y por bytes. Por segundos porque es lo que el
        usuario configura; por bytes porque el fps del pipeline es variable y a
        1600x1200 los mismos cinco segundos ocupan diez veces más.
        """
        self._pre.append((jpg, ts))
        self._pre_bytes += len(jpg)
        max_bytes = self._store.cfg.preroll_max_mb * 1024 ** 2
        while self._pre and (ts - self._pre[0][1] > self.cfg.pre_roll_sec
                             or self._pre_bytes > max_bytes):
            old, _ = self._pre.popleft()
            self._pre_bytes -= len(old)

    def _push(self, jpg: bytes, ts: float) -> None:
        """Encola un frame hacia el disco, sin bloquear jamás."""
        try:
            self._q.put_nowait((_FRAME, (jpg, ts)))
            self._frames += 1
        except queue.Full:
            # Se tira el frame NUEVO, no el viejo. En un fichero que se lee de
            # principio a fin, descartar el viejo descolocaría el orden
            # temporal; descartar el nuevo solo mete un salto y el clip sigue
            # siendo coherente.
            self._dropped += 1
            if self._dropped % 100 == 1:
                print(f"[{self._camera_id}] grabación: cola llena, "
                      f"{self._dropped} frame(s) descartados (¿disco lento?)")

    def _open(self, trigger: str, jpg: bytes, ts: float) -> None:
        self._pending_open = None
        size = jpeg_size(jpg)
        if size is None:
            self.last_error = "no se pudo leer el tamaño del JPEG"
            return
        if not self._store.has_room():
            self.last_error = f"sin espacio: menos de {self._store.cfg.min_free_gb} GB libres"
            print(f"[{self._camera_id}] grabación cancelada, {self.last_error}")
            return

        try:
            final = self._store.new_clip_path(self._camera_id, trigger, ts=ts)
        except OSError as e:
            self.last_error = f"no se pudo crear la carpeta del clip: {e!r}"
            print(f"[{self._camera_id}] {self.last_error}")
            return

        fps = self._resolve_fps()
        self._state = "recording"
        self._trigger = trigger
        self._clip_final = final
        self._clip_id = self._store.clip_id_of(final)
        self._started_at = ts
        self._frames = 0
        self._dropped = 0
        self._size = size
        self._last_det_ts = ts
        self._hits = 0
        self._store.mark_in_progress(self._clip_id)

        meta = {
            "clip_id": self._clip_id,
            "camera_id": self._camera_id,
            "trigger": trigger,
            "note": self._note,
            "started_at": ts,
            "fps": fps,
            "width": size[0],
            "height": size[1],
            "source": self.cfg.source,
            "pre_roll_sec": self.cfg.pre_roll_sec,
        }
        self._ensure_thread()
        self._q.put((_OPEN, _OpenMsg(final=final, part=part_path(final),
                                     width=size[0], height=size[1], fps=fps, meta=meta)))

        # La miniatura es el frame que disparó, tal cual: no cuesta codificar
        # nada y le da a HA una imagen para la tarjeta sin abrir el vídeo.
        if self._store.cfg.save_thumbnail:
            try:
                final.with_suffix(".jpg").write_bytes(jpg)
            except OSError as e:
                print(f"[{self._camera_id}] no se pudo guardar la miniatura: {e!r}")

        # El pre-roll entero va primero, en orden, y se TRANSFIERE (no se copia:
        # son referencias a bytes inmutables).
        pre, self._pre, self._pre_bytes = list(self._pre), deque(), 0
        for pre_jpg, pre_ts in pre:
            self._push(pre_jpg, pre_ts)
        self._push(jpg, ts)

        self._opened_evt.set()
        print(f"[{self._camera_id}] grabando {self._clip_id} "
              f"({trigger}, {size[0]}x{size[1]} @ {fps:.1f} fps, "
              f"{len(pre)} frames de pre-roll)")

    def _check_close(self, ts: float, jpg: bytes) -> None:
        """¿Toca cerrar este clip? Se mira una vez por frame."""
        # Cambio de resolución: el encoder ya está abierto con un tamaño fijo y
        # seguir metiéndole frames de otro produce basura. Se rota de clip.
        size = jpeg_size(jpg)
        if size is not None and self._size is not None and size != self._size:
            print(f"[{self._camera_id}] la cámara ha cambiado a {size[0]}x{size[1]}, "
                  f"se rota el clip")
            trigger = self._trigger
            self._close("cambio de resolución", ts=ts)
            self._pending_open = trigger or "detection"
            self._cooldown_until = 0.0
            return

        if ts - self._started_at >= self.cfg.max_clip_sec:
            trigger = self._trigger
            self._close("duración máxima", ts=ts)
            # Rotar, no parar: el evento sigue y el siguiente frame abre el
            # clip que lo continúa.
            self._pending_open = trigger
            self._cooldown_until = 0.0
            return

        if self._trigger == "manual":
            return  # el manual solo se cierra a mano o por duración máxima
        if self.cfg.trigger_on_detection and ts - self._last_det_ts >= self.cfg.post_roll_sec:
            self._close("fin del evento", ts=ts)

    def _close(self, reason: str, ts: Optional[float] = None, timeout: float = 5.0) -> None:
        """Cierra el clip. `ts` es el instante del frame que provoca el cierre.

        La duración se mide contra `_started_at`, que es el timestamp del frame
        que abrió: hay que restar sobre el MISMO reloj. El cooldown, en cambio,
        va contra `time.time()` porque quien lo consulta es `on_detections`, que
        no recibe timestamp de frame.
        """
        if self._state != "recording":
            return
        now = ts if ts is not None else time.time()
        duracion = max(0.0, now - self._started_at)
        # Un clip de medio segundo casi siempre es un falso positivo: ocupa
        # sitio en el disco y, sobre todo, en el listado que mira el usuario.
        descartar = duracion < self.cfg.min_clip_sec

        meta = {
            "ended_at": now,
            "duration_sec": round(duracion, 2),
            "frames": self._frames,
            "dropped_frames": self._dropped,
            "labels": dict(self._labels),
            "max_conf": round(self._max_conf, 3) if self._max_conf else None,
            "close_reason": reason,
            "encoder": self.encoder_name,
        }
        self._q.put((_CLOSE, _CloseMsg(meta=meta, discard=descartar, timeout=timeout)))

        print(f"[{self._camera_id}] clip {'descartado' if descartar else 'cerrado'} "
              f"{self._clip_id} ({reason}, {duracion:.1f}s, {self._frames} frames)")

        self._state = "idle"
        self._trigger = None
        self._clip_final = None
        self._clip_id = None
        self._started_at = 0.0
        self._labels = {}
        self._max_conf = 0.0
        self._note = None
        self._size = None
        self._hits = 0
        self._cooldown_until = time.time() + self.cfg.cooldown_sec

    def _resolve_fps(self) -> float:
        """FPS con el que se abre el fichero.

        Se fija al abrir y no se toca: un MP4 a fps variable se puede hacer,
        pero ni `VideoWriter` ni `image2pipe` dejan poner el PTS de cada frame
        sin complicarlo mucho. La deriva se corrige repitiendo frames en el hilo
        escritor (ver `frames_for_gap`).
        """
        if self.cfg.fps:
            return float(self.cfg.fps)
        medido = 0.0
        try:
            medido = float(self._fps_getter() or 0.0)
        except Exception:
            pass
        if medido <= 0:
            return 12.0
        g = self._store.cfg
        return max(g.fps_min, min(g.fps_max, medido))

    # -- el hilo escritor ----------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._writer_loop,
                                        name=f"rec-{self._camera_id}", daemon=True)
        self._thread.start()

    def _stop_thread(self, timeout: float = 2.0) -> None:
        t, self._thread = self._thread, None
        if t is None:
            return
        try:
            self._q.put_nowait((_QUIT, None))
        except queue.Full:
            pass
        t.join(timeout=timeout)

    def _make_encoder(self) -> tuple[VideoEncoder, str]:
        if self._encoder_factory is not None:
            return self._encoder_factory()
        g = self._store.cfg
        return resolve_encoder(g.encoder, g.ffmpeg_path, g.fourcc, g.crf, g.preset)

    def _writer_loop(self) -> None:
        """El único hilo que toca el encoder y el disco.

        Todo lo que puede ir lento —arrancar ffmpeg, escribir, esperar el
        cierre— pasa aquí, donde bloquear no le hace daño a nadie.
        """
        enc: Optional[VideoEncoder] = None
        open_msg: Optional[_OpenMsg] = None
        last_ts = 0.0
        last_jpeg: Optional[bytes] = None
        written = 0

        while True:
            kind, payload = self._q.get()

            if kind == _QUIT:
                break

            try:
                if kind == _OPEN:
                    open_msg = payload
                    enc, motivo = self._make_encoder()
                    self.encoder_name = enc.name
                    if not getattr(enc, "playable_in_browser", True):
                        open_msg.meta["playable_in_browser"] = False
                    else:
                        open_msg.meta["playable_in_browser"] = True
                    open_msg.meta["encoder"] = enc.name
                    open_msg.meta["encoder_reason"] = motivo
                    enc.open(open_msg.part, open_msg.width, open_msg.height, open_msg.fps)
                    last_ts, last_jpeg, written = 0.0, None, 0

                elif kind == _FRAME and enc is not None and open_msg is not None:
                    jpg, ts = payload
                    # Relleno: si entre este frame y el anterior ha pasado más
                    # que un periodo del fichero, se repite el anterior para que
                    # el reloj del vídeo siga al reloj real.
                    if last_jpeg is not None and last_ts:
                        extra = frames_for_gap(ts - last_ts, open_msg.fps) - 1
                        for _ in range(extra):
                            enc.write(last_jpeg)
                            written += 1
                    enc.write(jpg)
                    written += 1
                    last_ts, last_jpeg = ts, jpg

                elif kind == _CLOSE:
                    self._finish(enc, open_msg, payload, written)
                    enc, open_msg, last_jpeg = None, None, None

            except Exception as e:
                self.last_error = repr(e)
                print(f"[{self._camera_id}] grabación: fallo en el hilo escritor: {e!r}")
                # El clip está roto, pero el grabador tiene que seguir vivo para
                # el siguiente evento: se cierra lo que haya y se sigue.
                try:
                    if enc is not None:
                        enc.close(timeout=2.0)
                except Exception:
                    pass
                if open_msg is not None:
                    self._store.unmark_in_progress(open_msg.meta["clip_id"])
                enc, open_msg, last_jpeg = None, None, None
                self._closed_evt.set()
            finally:
                self._q.task_done()

        if enc is not None:
            try:
                enc.close(timeout=1.5)
            except Exception:
                pass

    def _finish(self, enc: Optional[VideoEncoder], open_msg: Optional[_OpenMsg],
                msg: _CloseMsg, written: int) -> None:
        """Cierra el encoder y publica el clip: .part -> .mp4 + sidecar."""
        if enc is None or open_msg is None:
            self._closed_evt.set()
            return

        enc.close(timeout=msg.timeout)

        meta = {**open_msg.meta, **msg.meta, "frames_written": written}
        clip_id = open_msg.meta["clip_id"]
        part, final = open_msg.part, open_msg.final

        try:
            if msg.discard or not part.is_file() or part.stat().st_size == 0:
                part.unlink(missing_ok=True)
                final.with_suffix(".jpg").unlink(missing_ok=True)
                self.last_clip = None
            else:
                meta["bytes"] = part.stat().st_size
                # Atómico: hasta esta línea el fichero no existe con su nombre
                # final, así que ni el listado ni la retención ni Home Assistant
                # pueden encontrarse un MP4 a medio escribir.
                part.replace(final)
                write_sidecar(final, meta)
                self.last_clip = meta
                self.clips_written += 1
        except OSError as e:
            self.last_error = f"no se pudo publicar el clip: {e!r}"
            print(f"[{self._camera_id}] {self.last_error}")
        finally:
            self._store.unmark_in_progress(clip_id)
            self._closed_evt.set()

        # Un barrido justo después de cerrar: entre barridos periódicos pueden
        # pasar diez minutos, y una noche movida cabe de sobra ahí dentro.
        try:
            self._store.sweep()
        except Exception as e:
            print(f"[{self._camera_id}] retención tras el clip: {e!r}")
