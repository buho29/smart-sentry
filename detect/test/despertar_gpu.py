"""¿Se puede sacar a la GTX 1080 de P5 sin gastar una inferencia entera?

Con yolo26n la tarjeta baja a P5 (759 MHz) porque el driver la ve ociosa
(`gpu_idle`), y a esos relojes la inferencia sale corrupta (ver docs/GPU.md).
Ni el driver, ni la BIOS, ni una app 3D abierta lo evitan, y el keep-alive con
inferencias dummy costaba casi lo mismo que pasar a yolo26m.

La hipótesis que se mide aquí: la "utilización" que mira el driver es el
porcentaje de tiempo en que HAY algún kernel corriendo, no cuánto trabaja.
`torch.cuda._sleep(ciclos)` lanza un kernel de un solo hilo que solo cuenta
ciclos. Encadenado sin huecos en un stream propio, el driver vería la GPU
ocupada todo el rato mientras casi todos los SM quedan libres para YOLO, y el
consumo debería ser mínimo.

Se lanza en trozos de ~10 ms: en Windows un kernel que pase de 2 s dispara el
TDR y el driver se reinicia.

Uso (con el servicio PARADO, para que no haya otra carga en la GPU):

    cd detect
    venv\\Scripts\\python.exe test\\despertar_gpu.py sin-relleno
    venv\\Scripts\\python.exe test\\despertar_gpu.py spin
    venv\\Scripts\\python.exe test\\despertar_gpu.py medium
"""

from __future__ import annotations

import argparse
import csv
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import pynvml  # noqa: E402
import torch  # noqa: E402
from ultralytics.utils import ASSETS  # noqa: E402

from camera import get_model  # noqa: E402

# Igual que CUDNN_ENABLED en main.py: hay que medir en las mismas condiciones
# en las que corre el servicio.
torch.backends.cudnn.enabled = False

FRAME_PERIOD_SEC = 0.060      # 16,7 fps, el ritmo de la cámara
# Fino a propósito: cada frame se etiqueta con la última muestra, y hay que
# poder ver si los errores caen justo después de un cambio de P-state.
SAMPLE_PERIOD_SEC = 0.1
# "Cerca de un cambio de P-state" para el desglose por transición.
NEAR_CHANGE_SEC = 2.0
# ~8 ms a 1900 MHz, ~20 ms a 759 MHz: siempre muy lejos de los 2 s del TDR.
SPIN_CYCLES = 15_000_000
GPU_IDLE_BIT = 0x1            # nvmlClocksEventReasonGpuIdle


class Sample(NamedTuple):
    t: float            # time.monotonic()
    pstate: int
    gr_mhz: int         # reloj del chip
    mem_mhz: int        # reloj de la memoria
    power_w: float
    idle: bool          # el driver baja relojes por verla ociosa
    last_change: float  # time.monotonic() del último cambio de P-state


class FrameRecord(NamedTuple):
    t: float
    pstate: int
    gr_mhz: int
    mem_mhz: int
    since_change: float
    has_boxes: bool
    corrupt: int
    ms: float
    # Desglose de ultralytics: preproceso y posproceso van en la CPU, la
    # inferencia en la GPU. Sirve para ver cuál de las dos se encarece.
    pre_ms: float = 0.0
    inf_ms: float = 0.0
    post_ms: float = 0.0


class GpuProbe:
    """Muestrea P-state, relojes, consumo y el motivo `gpu_idle` en un hilo."""

    def __init__(self):
        pynvml.nvmlInit()
        self._h = pynvml.nvmlDeviceGetHandleByIndex(0)
        self.samples: list[Sample] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def latest(self) -> Sample | None:
        return self.samples[-1] if self.samples else None

    def _reasons(self) -> int:
        fn = getattr(pynvml, "nvmlDeviceGetCurrentClocksEventReasons", None) \
            or pynvml.nvmlDeviceGetCurrentClocksThrottleReasons
        return fn(self._h)

    def _run(self):
        last_change = time.monotonic()
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                pstate = pynvml.nvmlDeviceGetPerformanceState(self._h)
                prev = self.latest()
                if prev is not None and prev.pstate != pstate:
                    last_change = now
                self.samples.append(Sample(
                    t=now,
                    pstate=pstate,
                    gr_mhz=pynvml.nvmlDeviceGetClockInfo(self._h, pynvml.NVML_CLOCK_GRAPHICS),
                    mem_mhz=pynvml.nvmlDeviceGetClockInfo(self._h, pynvml.NVML_CLOCK_MEM),
                    power_w=pynvml.nvmlDeviceGetPowerUsage(self._h) / 1000.0,
                    idle=bool(self._reasons() & GPU_IDLE_BIT),
                    last_change=last_change,
                ))
            except pynvml.NVMLError as e:
                print(f"  (NVML falló: {e})")
            self._stop.wait(SAMPLE_PERIOD_SEC)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()


class Spinner:
    """Mantiene siempre un kernel `_sleep` en cola en un stream aparte."""

    def __init__(self, cycles: int, pause_ms: float):
        self._cycles = cycles
        self._pause_sec = pause_ms / 1000.0
        self._stream = torch.cuda.Stream()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.launches = 0

    def _run(self):
        # El stream actual es por hilo: esto no afecta al de YOLO.
        with torch.cuda.stream(self._stream):
            while not self._stop.is_set():
                torch.cuda._sleep(self._cycles)
                self._stream.synchronize()
                self.launches += 1
                # Hueco sin relleno: ¿basta con ocupar la GPU parte del tiempo?
                # time.sleep y no self._stop.wait: en Windows la espera de un
                # Event no baja de ~15,6 ms, y el hueco de 1 ms salía de 15.
                if self._pause_sec > 0:
                    time.sleep(self._pause_sec)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()


class GlKeeper:
    """Una ventana OpenGL mínima dibujando a ~60 fps en un hilo aparte.

    La idea: *Prefer maximum performance* del perfil de python.exe es un
    ajuste pensado para aplicaciones 3D, y el servicio solo usa CUDA. Si el
    mismo proceso presenta frames OpenGL, el driver quizá lo trate como un
    juego y no baje a P5.
    """

    _VERTEX = """
        #version 330
        in vec2 pos;
        void main() { gl_Position = vec4(pos, 0.0, 1.0); }
    """
    _FRAGMENT = """
        #version 330
        out vec4 color;
        void main() { color = vec4(0.2, 0.6, 0.3, 1.0); }
    """

    def __init__(self, visible: bool, max_fps: float = 0.0):
        self._visible = visible
        # 0 = lo que dé el vsync (la frecuencia del monitor). Con un tope más
        # bajo se ve si el driver sigue tratándolo como app 3D dibujando menos.
        self._frame_sec = 1.0 / max_fps if max_fps > 0 else 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self.error: str | None = None
        self.frames = 0
        self.seconds = 0.0

    def _run(self):
        import glfw
        import moderngl
        import numpy as np

        if not glfw.init():
            self.error = "glfw.init() falló"
            self._ready.set()
            return
        try:
            glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
            glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
            glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
            glfw.window_hint(glfw.VISIBLE, glfw.TRUE if self._visible else glfw.FALSE)
            window = glfw.create_window(64, 64, "despertar_gpu", None, None)
            if not window:
                self.error = "no se pudo crear la ventana OpenGL"
                return
            glfw.make_context_current(window)
            glfw.swap_interval(1)          # vsync: ~60 fps, no a tope
            ctx = moderngl.create_context()
            prog = ctx.program(vertex_shader=self._VERTEX, fragment_shader=self._FRAGMENT)
            vbo = ctx.buffer(np.array([-0.8, -0.8, 0.8, -0.8, 0.0, 0.8], dtype="f4").tobytes())
            vao = ctx.vertex_array(prog, [(vbo, "2f", "pos")])
            print(f"  (OpenGL en {ctx.info['GL_RENDERER']}, ventana "
                  f"{'visible' if self._visible else 'oculta'})")
            self._ready.set()
            t0 = time.perf_counter()
            while not self._stop.is_set() and not glfw.window_should_close(window):
                ctx.clear(0.05, 0.05, 0.05)
                vao.render()
                glfw.swap_buffers(window)
                glfw.poll_events()
                self.frames += 1
                if self._frame_sec:
                    # time.sleep y no Event.wait: en Windows este no baja de ~15,6 ms.
                    time.sleep(max(0.0, t0 + self.frames * self._frame_sec - time.perf_counter()))
            self.seconds = time.perf_counter() - t0
            glfw.destroy_window(window)
        except Exception as e:  # noqa: BLE001 - es una prueba: se informa y sigue
            self.error = repr(e)
        finally:
            self._ready.set()
            glfw.terminate()

    def start(self):
        self._thread.start()
        self._ready.wait(10)
        if self.error:
            print(f"  (OpenGL no arrancó: {self.error})")

    def stop(self):
        self._stop.set()
        self._thread.join()


def _error_line(label: str, frames: list[FrameRecord]) -> str:
    """Una fila del desglose: frames corruptos y frames sin cajas por cada 1000.

    Se cuentan frames y no cajas: un frame roto puede traer cientos de cajas
    imposibles, y contarlas hacía parecer mil fallos lo que eran tres.
    """
    n = len(frames)
    if not n:
        return f"  {label:<28} sin frames"
    corrupt = sum(f.corrupt > 0 for f in frames)
    no_boxes = sum(not f.has_boxes for f in frames)
    return (f"  {label:<28} {n:>6} frames | corruptos {1000 * corrupt / n:6.1f}/1000 | "
            f"sin cajas {1000 * no_boxes / n:7.1f}/1000 | "
            f"GPU {statistics.mean(f.inf_ms for f in frames):5.1f} ms | "
            f"chip {statistics.mean(f.gr_mhz for f in frames):5.0f} MHz, "
            f"memoria {statistics.mean(f.mem_mhz for f in frames):5.0f} MHz")


def print_breakdown(frames: list[FrameRecord]) -> None:
    """Separa las tres sospechas: memoria (P5), reloj del chip y transiciones."""
    frames = [f for f in frames if f.pstate >= 0]
    print("\n--- Por P-state ---")
    for p in sorted({f.pstate for f in frames}):
        print(_error_line(f"P{p}", [f for f in frames if f.pstate == p]))

    # Dentro de P5 la memoria está fija y el chip se mueve: si el error no
    # cambia con el reloj del chip, no es el chip.
    p5 = [f for f in frames if f.pstate == 5]
    print("\n--- Dentro de P5, por reloj del chip ---")
    for label, lo, hi in (("chip < 850 MHz", 0, 850), ("chip 850-1000 MHz", 850, 1000),
                          ("chip > 1000 MHz", 1000, 10_000)):
        print(_error_line(label, [f for f in p5 if lo <= f.gr_mhz < hi]))

    print("\n--- Por cercanía a un cambio de P-state ---")
    near = [f for f in frames if f.since_change < NEAR_CHANGE_SEC]
    print(_error_line(f"< {NEAR_CHANGE_SEC:g} s del cambio", near))
    print(_error_line("estable", [f for f in frames if f.since_change >= NEAR_CHANGE_SEC]))
    print(_error_line("P5 estable", [f for f in p5 if f.since_change >= NEAR_CHANGE_SEC]))


def write_csv(path: Path, frames: list[FrameRecord], t0: float) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t_sec", "pstate", "gr_mhz", "mem_mhz", "since_change_sec",
                    "has_boxes", "corrupt", "ms"])
        for f in frames:
            w.writerow([f"{f.t - t0:.3f}", f.pstate, f.gr_mhz, f.mem_mhz,
                        f"{f.since_change:.2f}", int(f.has_boxes), f.corrupt, f"{f.ms:.1f}"])
    print(f"\nFrames volcados en {path}")


def infer_once(model, frame, imgsz: int, probe: GpuProbe) -> FrameRecord:
    """Una inferencia, etiquetada con la última muestra de la GPU (≤ 0,1 s)."""
    t0 = time.perf_counter()
    result = model.predict(frame, imgsz=imgsz, conf=0.25, verbose=False)[0]
    ms = (time.perf_counter() - t0) * 1000
    confs = result.boxes.conf.tolist()
    bad = [c for c in confs if not 0.0 <= c <= 1.0]
    if bad:
        print(f"  [{time.strftime('%H:%M:%S')}] CORRUPTA: confianza {max(bad, key=abs):.3f}")
    now = time.monotonic()
    s = probe.latest()
    return FrameRecord(
        t=now,
        pstate=s.pstate if s else -1,
        gr_mhz=s.gr_mhz if s else 0,
        mem_mhz=s.mem_mhz if s else 0,
        since_change=(now - s.last_change) if s else 0.0,
        has_boxes=bool(confs),
        corrupt=len(bad),
        ms=ms,
        pre_ms=result.speed.get("preprocess", 0.0),
        inf_ms=result.speed.get("inference", 0.0),
        post_ms=result.speed.get("postprocess", 0.0),
    )


def run(mode: str, seconds: float, cycles: int, pause_ms: float,
        csv_path: Path | None = None, model_name: str = "yolo26n",
        imgsz: int = 640, cameras: int = 1, gl_visible: bool = True,
        gl_fps: float = 0.0, gl_separate: bool = False) -> None:
    if mode == "medium":
        model_name = "yolo26m"
    # Un modelo por "cámara", como en el servicio (get_model con owner): cada
    # una tiene su instancia y todas infieren en cada periodo de 60 ms.
    models = [get_model(model_name, "cuda", owner=f"cam{i}") for i in range(cameras)]
    frame = cv2.imread(str(ASSETS / "bus.jpg"))
    if frame is None:
        sys.exit(f"No encuentro la imagen de prueba {ASSETS / 'bus.jpg'}")

    print(f"Modo {mode}: {cameras} x {model_name} a {imgsz}, "
          f"{1 / FRAME_PERIOD_SEC:.1f} fps durante {seconds:.0f} s")

    probe = GpuProbe()
    spinner = Spinner(cycles, pause_ms) if mode == "spin" else None
    gl_keeper = GlKeeper(gl_visible, gl_fps) if mode == "gl" and not gl_separate else None
    gl_proc = None
    probe.start()
    if spinner:
        spinner.start()
    if gl_keeper:
        gl_keeper.start()
    if mode == "gl" and gl_separate:
        # Otro python.exe (mismo ejecutable, así que mismo perfil del driver)
        # con solo la ventana: CUDA y OpenGL en procesos distintos.
        gl_proc = subprocess.Popen(
            [sys.executable, __file__, "gl", "--solo-gl",
             "--gl-ventana", "visible" if gl_visible else "oculta", "--gl-fps", str(gl_fps)])
        time.sleep(3)   # que cree el contexto antes de empezar a medir
        print(f"  (OpenGL en proceso aparte, pid {gl_proc.pid})")

    records: list[FrameRecord] = []
    t_start = time.monotonic()
    t_end = t_start + seconds
    next_frame = t_start
    try:
        while time.monotonic() < t_end:
            for model in models:
                records.append(infer_once(model, frame, imgsz, probe))
            next_frame += FRAME_PERIOD_SEC
            time.sleep(max(0.0, next_frame - time.monotonic()))
    finally:
        if spinner:
            spinner.stop()
        if gl_keeper:
            gl_keeper.stop()
        if gl_proc:
            gl_proc.terminate()
            gl_proc.wait(10)
        probe.stop()

    elapsed = time.monotonic() - t_start
    times_ms = sorted(r.ms for r in records)
    corrupt_frames = sum(r.corrupt > 0 for r in records)
    corrupt_boxes = sum(r.corrupt for r in records)
    frames_with_boxes = sum(r.has_boxes for r in records)
    pstates = Counter(s.pstate for s in probe.samples)
    n = len(probe.samples) or 1
    print("\n=== RESUMEN ===")
    print(f"Modo: {mode} ({cameras} x {model_name} a {imgsz})")
    # Lo que decidiría el relleno en el servicio: qué parte del tiempo está la
    # GPU trabajando para nosotros (suma de ms de inferencia entre el tiempo).
    print(f"Carga propia: {100 * sum(times_ms) / 1000 / elapsed:.0f} % del tiempo")
    print("P-states: " + ", ".join(f"P{p} {100 * c / n:.0f} %" for p, c in sorted(pstates.items())))
    print(f"Reloj medio: {statistics.mean(s.gr_mhz for s in probe.samples):.0f} MHz "
          f"(memoria {statistics.mean(s.mem_mhz for s in probe.samples):.0f} MHz)")
    print(f"Consumo medio: {statistics.mean(s.power_w for s in probe.samples):.1f} W")
    print(f"gpu_idle activo: {100 * sum(s.idle for s in probe.samples) / n:.0f} % de las muestras")
    if records:
        print(f"Inferencia: media {statistics.mean(times_ms):.1f} ms, "
              f"p95 {times_ms[int(0.95 * (len(times_ms) - 1))]:.1f} ms ({len(times_ms)} frames)")
        print(f"  desglose medio: preproceso {statistics.mean(r.pre_ms for r in records):.1f} ms, "
              f"GPU {statistics.mean(r.inf_ms for r in records):.1f} ms, "
              f"posproceso {statistics.mean(r.post_ms for r in records):.1f} ms")
        print(f"Frames corruptos: {corrupt_frames} ({corrupt_boxes} cajas imposibles); "
              f"frames con cajas: {frames_with_boxes} de {len(records)}")
    else:
        print("Sin inferencias (--camaras 0): solo se mide la GPU.")
    if spinner:
        print(f"Kernels de relleno lanzados: {spinner.launches} "
              f"({cycles:_} ciclos, pausa {pause_ms:g} ms)")
    if gl_keeper:
        fps = gl_keeper.frames / gl_keeper.seconds if gl_keeper.seconds else 0.0
        print(f"OpenGL: {gl_keeper.frames} frames dibujados ({fps:.0f} fps)"
              + (f", error: {gl_keeper.error}" if gl_keeper.error else ""))
    print_breakdown(records)
    if csv_path:
        write_csv(csv_path, records, t_start)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mide si un kernel mínimo saca a la GTX 1080 de P5 con yolo26n.")
    parser.add_argument("modo", choices=["sin-relleno", "spin", "gl", "medium"],
                        help="sin-relleno: solo YOLO. spin: YOLO más el kernel de relleno. "
                             "gl: YOLO más una ventana OpenGL dibujando a ~60 fps. "
                             "medium: yolo26m, como referencia.")
    parser.add_argument("--gl-ventana", choices=["visible", "oculta"], default="visible",
                        help="Modo gl: ventana visible (se puede minimizar) u oculta. Una "
                             "oculta puede que no presente frames. Por defecto visible.")
    parser.add_argument("--gl-fps", type=float, default=0.0,
                        help="Modo gl: tope de fps de la ventana. 0 = lo que dé el vsync "
                             "(la frecuencia del monitor). Por defecto 0.")
    parser.add_argument("--segundos", type=float, default=120.0,
                        help="Duración de la medida. Por defecto 120.")
    parser.add_argument("--ciclos", type=int, default=SPIN_CYCLES,
                        help=f"Ciclos de cada kernel de relleno (modo spin). Más cortos "
                             f"retrasan menos a YOLO pero se lanzan más veces. "
                             f"Por defecto {SPIN_CYCLES:_}.")
    parser.add_argument("--pausa-ms", type=float, default=0.0,
                        help="Hueco sin relleno entre un kernel y el siguiente (modo spin). "
                             "0 = relleno continuo. Por defecto 0.")
    parser.add_argument("--csv", type=Path, default=None,
                        help="Vuelca cada frame con su P-state, relojes y errores a este "
                             "fichero CSV, para revisarlo a mano.")
    parser.add_argument("--modelo", default="yolo26n",
                        help="Pesos YOLO sin el .pt (yolo26n, yolo26s, yolo26m...). "
                             "El modo medium fuerza yolo26m. Por defecto yolo26n.")
    parser.add_argument("--imgsz", type=int, default=640,
                        help="Resolución de inferencia. Por defecto 640.")
    parser.add_argument("--camaras", type=int, default=1,
                        help="Cuántas cámaras simular: cada una con su modelo e inferencia "
                             "en cada periodo de 60 ms. Por defecto 1.")
    parser.add_argument("--gl-proceso", choices=["mismo", "aparte"], default="mismo",
                        help="Modo gl: la ventana en el mismo proceso que YOLO o en otro "
                             "python.exe. Por defecto mismo.")
    # Interno: lo usa --gl-proceso aparte para lanzar solo la ventana.
    parser.add_argument("--solo-gl", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.solo_gl:
        keeper = GlKeeper(args.gl_ventana == "visible", args.gl_fps)
        keeper.start()
        keeper._thread.join()   # hasta que el proceso padre lo termine
        return
    if not torch.cuda.is_available():
        sys.exit("No hay CUDA: esta prueba solo tiene sentido en la GPU.")
    run(args.modo, args.segundos, args.ciclos, args.pausa_ms, args.csv,
        args.modelo, args.imgsz, args.camaras, args.gl_ventana == "visible",
        args.gl_fps, args.gl_proceso == "aparte")


if __name__ == "__main__":
    main()
