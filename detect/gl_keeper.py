"""GL keeper: una ventana OpenGL oculta para que la GTX 1080 no baje a P5.

Con yolo26n la GTX 1080 pasa gran parte del tiempo ociosa entre frames y el
driver la baja a P5 (~760 MHz). Ahí el nano deja de ver a la persona en casi
todos los frames, y en los cambios de P-state salen confianzas imposibles. Con
una cámara la solución es yolo26m; con dos o más, varios yolo26m se comen la
GPU y hace falta el nano. Ver docs/GPU.md.

Lo que lo arregla, medido con test/despertar_gpu.py (26/09/2026): que haya un
**python.exe con un contexto OpenGL abierto** y que el perfil de `python.exe`
en NVIDIA Profile Inspector tenga **Power Management = Prefer maximum
performance** (con Force P2 = On se queda en P2). El driver trata entonces ese
proceso como una aplicación 3D y mantiene la GPU a 1657 MHz fijos: 3000 de 3000
frames con detección, 0 corruptos, ~53 W. Da igual cuánto se dibuje (con un
frame por minuto basta) y no encarece la inferencia. **Sin ese ajuste del
perfil no hace nada**: se comprueba con `nvidia-smi` (P0/P2 fijo, `gpu_idle`
Not Active).

La ventana va en un **proceso hijo** y no en un hilo del servicio: el driver
deja el proceso en P0 mientras viva, aunque se cierre la ventana (medido), así
que la única forma de soltar la GPU cuando las cámaras duermen es terminar el
proceso. Con él cerrado la GPU vuelve a reposo (P8) en ~15 s. El hijo solo se
lanza mientras hay alguna cámara funcionando en CUDA, y se muere solo si el
servicio desaparece (su stdin se cierra).

Cómo quitarlo:
  1. borrar este fichero;
  2. en main.py, las líneas marcadas con `# gl_keeper`;
  3. en camera.py, `gl_keeper_enabled` de `GlobalConfig`;
  4. en requirements.txt, `glfw` y `moderngl`;
  5. en los tests, las comprobaciones de `gl_keeper`.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# Cada cuánto se revisa si hace falta la ventana.
CHECK_INTERVAL_SEC = 2.0

# Frames por segundo que dibuja la ventana. Lo que cuenta es que el contexto
# exista: 1 fps sobra (medido igual con 1 frame por minuto que con 165 fps).
DRAW_INTERVAL_SEC = 1.0

# Si el hijo muere antes de esto al arrancar, es que no puede abrir la ventana
# (faltan glfw/moderngl, no hay OpenGL...): se avisa y no se reintenta.
EARLY_EXIT_SEC = 5.0


def _should_run(enabled: bool, needed: bool) -> bool:
    """¿Tiene que estar abierta la ventana? Función pura para probarla sin GPU."""
    return enabled and needed


class GlKeeper:
    """Lanza y termina el proceso de la ventana según haga falta, desde un hilo."""

    def __init__(self, is_needed: Callable[[], bool]):
        self._is_needed = is_needed
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._proc_started = 0.0
        self._error: Optional[str] = None

    # Import perezoso: el proceso hijo carga este módulo y no debe arrastrar
    # camera.py (torch, ultralytics) solo para abrir una ventana.
    @staticmethod
    def _enabled() -> bool:
        from camera import GLOBAL_CONFIG
        return GLOBAL_CONFIG.gl_keeper_enabled

    def status(self) -> dict:
        return {
            "enabled": self._enabled(),
            "active": self._proc is not None and self._proc.poll() is None,
            "error": self._error,
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="gl-keeper", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._kill()

    def retry(self) -> None:
        """Olvida un fallo anterior para volver a intentarlo (al reactivarlo)."""
        self._error = None

    def _wants(self) -> bool:
        try:
            needed = bool(self._is_needed())
        except Exception:  # noqa: BLE001 - un fallo aquí no puede tumbar el hilo
            needed = False
        return _should_run(self._enabled(), needed)

    def _run(self) -> None:
        from log import print

        while not self._stop.is_set():
            alive = self._proc is not None and self._proc.poll() is None
            if self._proc is not None and not alive:
                # El hijo ha muerto por su cuenta.
                if time.monotonic() - self._proc_started < EARLY_EXIT_SEC:
                    self._error = (f"el proceso de la ventana GL terminó al arrancar "
                                   f"(código {self._proc.returncode}); mira el log")
                    print(f"gl_keeper: desactivado, {self._error}")
                self._proc = None
            if self._wants() and self._error is None:
                if self._proc is None:
                    self._launch()
                    print("gl_keeper: ventana GL abierta (cámaras en CUDA activas)")
            elif self._proc is not None:
                self._kill()
                print("gl_keeper: ventana GL cerrada")
            self._stop.wait(CHECK_INTERVAL_SEC)

    def _launch(self) -> None:
        # Mismo python.exe que el servicio: el perfil del driver va por nombre
        # de ejecutable. stdin en tubería para que el hijo sepa si seguimos vivos.
        self._proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child"],
            stdin=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._proc_started = time.monotonic()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Proceso hijo: solo la ventana
# ---------------------------------------------------------------------------

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


def _child_main() -> int:
    """Abre la ventana oculta y dibuja hasta que el padre desaparezca."""
    try:
        import glfw
        import moderngl
        import numpy as np
    except ImportError as e:
        print(f"gl_keeper: faltan glfw o moderngl ({e}); "
              f"instálalos o apaga POST /config/gl-keeper", flush=True)
        return 2

    stop = threading.Event()

    def watch_parent():
        # Cuando el servicio muere (o nos termina), su extremo del tubo se
        # cierra y read() devuelve vacío.
        try:
            sys.stdin.buffer.read()
        finally:
            stop.set()

    threading.Thread(target=watch_parent, daemon=True).start()

    if not glfw.init():
        print("gl_keeper: glfw.init() falló", flush=True)
        return 3
    try:
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
        window = glfw.create_window(64, 64, "cupula gl_keeper", None, None)
        if not window:
            print("gl_keeper: no se pudo crear la ventana OpenGL", flush=True)
            return 4
        glfw.make_context_current(window)
        ctx = moderngl.create_context()
        prog = ctx.program(vertex_shader=_VERTEX, fragment_shader=_FRAGMENT)
        vbo = ctx.buffer(np.array([-0.8, -0.8, 0.8, -0.8, 0.0, 0.8], dtype="f4").tobytes())
        vao = ctx.vertex_array(prog, [(vbo, "2f", "pos")])
        while not stop.is_set():
            ctx.clear(0.05, 0.05, 0.05)
            vao.render()
            glfw.swap_buffers(window)
            glfw.poll_events()
            stop.wait(DRAW_INTERVAL_SEC)
        glfw.destroy_window(window)
        return 0
    finally:
        glfw.terminate()


if __name__ == "__main__" and "--child" in sys.argv:
    sys.exit(_child_main())
