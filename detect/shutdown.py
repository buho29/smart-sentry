"""Aviso temprano de apagado del servicio.

Vive aparte de main.py porque lo consume el generador MJPEG (camera.py), y
main.py solo se encarga de instalar el hook al arrancar el lifespan.
"""

import signal
import sys
import threading


# ---------------------------------------------------------------------------
# Aviso temprano de apagado
# ---------------------------------------------------------------------------
#
# uvicorn apaga en tres pasos, en este orden (uvicorn/server.py, Server.shutdown):
#   1. deja de aceptar conexiones nuevas;
#   2. ESPERA a que terminen las respuestas en vuelo, como mucho
#      --timeout-graceful-shutdown segundos, y si expira las cancela a la
#      fuerza ("Cancel N running task(s), timeout graceful shutdown exceeded");
#   3. solo entonces ejecuta el shutdown del lifespan, que es donde nosotros
#      paramos las cámaras.
#
# Nuestros streams MJPEG son bucles infinitos que solo salían cuando el
# lifespan marcaba _stop_event, o sea en el paso 3 -- que no llega hasta que el
# paso 2 se rinde. Bloqueo circular: con clientes conectados, cada Ctrl+C
# costaba los 5s enteros y soltaba un CancelledError por cliente.
#
# Como uvicorn instala sus handlers de señal ANTES de arrancar el lifespan
# (Server.serve: `with self.capture_signals(): await self._serve(...)`), desde
# el arranque del lifespan podemos encadenarnos a ellos y enterarnos de la
# señal en el "paso 0". Los generadores miran esta bandera y salen solos, así
# que el paso 2 termina enseguida en vez de agotar el timeout.

_SHUTTING_DOWN = False

# Las mismas que captura uvicorn (server.py, HANDLED_SIGNALS): si nos
# encadenáramos a menos, un Ctrl+Break apagaría el servidor sin que los
# generadores se enteraran, que es justo el problema que esto arregla.
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)
if sys.platform == "win32":
    _SHUTDOWN_SIGNALS += (signal.SIGBREAK,)  # Ctrl+Break


def is_shutting_down() -> bool:
    return _SHUTTING_DOWN


def _install_shutdown_signal_hook():
    """Encadena un handler propio a los que ya instaló uvicorn (ver arriba)."""
    if threading.current_thread() is not threading.main_thread():
        return  # signal.signal solo funciona desde el hilo principal

    for sig in _SHUTDOWN_SIGNALS:
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):
            continue

        def handler(signum, frame, _previous=previous):
            # Ojo: esto corre dentro de un handler de señal. Una asignación a
            # un bool de módulo es atómica y segura aquí; un threading.Event
            # no lo sería del todo, porque set() coge un lock.
            global _SHUTTING_DOWN
            _SHUTTING_DOWN = True
            # Delegamos en el handler de uvicorn (Server.handle_exit), que es
            # quien de verdad arranca el apagado ordenado.
            if callable(_previous):
                _previous(signum, frame)

        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # p.ej. SIGTERM no soportado en esta plataforma
