"""Salida por consola del servicio.

Todos los mensajes salen por print(). Se envuelve una sola vez aqui para que
lleven la hora en el mismo formato que el log de ESPHome ([HH:MM:SS.mmm]) y se
puedan cotejar los dos logs. Los demas modulos hacen `from log import print`,
asi que ninguna llamada cambia.
"""

import time


# Todos los mensajes de este servicio salen por print(). Lo envolvemos una
# sola vez para anteponer la hora local en el mismo formato que usa el log de
# ESPHome ([HH:MM:SS.mmm]), y así poder cotejar tiempos entre ambos logs.
_orig_print = print


def print(*args, **kwargs):  # noqa: A001
    t = time.time()
    ts = time.strftime("%H:%M:%S", time.localtime(t))
    _orig_print(f"[{ts}.{int(t % 1 * 1000):03d}]", *args, **kwargs)
