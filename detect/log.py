"""Salida por consola del servicio.

Todos los mensajes salen por print(). Se envuelve una sola vez aqui para que
lleven la hora en el mismo formato que el log de ESPHome ([HH:MM:SS.mmm]) y se
puedan cotejar los dos logs. Los demas modulos hacen `from log import print`,
asi que ninguna llamada cambia.

Opcionalmente (enable_file) cada linea se copia ademas a un fichero que rota
por tamaño. Solo lo activa supervisor.py; sin esa llamada no se escribe nada
a disco.
"""

import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Optional


# Todos los mensajes de este servicio salen por print(). Lo envolvemos una
# sola vez para anteponer la hora local en el mismo formato que usa el log de
# ESPHome ([HH:MM:SS.mmm]), y así poder cotejar tiempos entre ambos logs.
_orig_print = print

# Copia a fichero: None hasta que alguien llame a enable_file()
_file_logger: Optional[logging.Logger] = None


def enable_file(path: Path, max_bytes: int, backups: int):
    """Copia a `path` todo lo que salga por print()/write_raw(). Al llegar a
    `max_bytes` rota a path.1, path.2... y guarda como mucho `backups` copias,
    así que en disco nunca hay más de (backups + 1) * max_bytes."""
    global _file_logger
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("cupula.file")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _file_logger = logger


def print(*args, sep=" ", **kwargs):  # noqa: A001
    t = time.time()
    ts = time.strftime("%H:%M:%S", time.localtime(t))
    stamp = f"[{ts}.{int(t % 1 * 1000):03d}]"
    _orig_print(stamp, *args, sep=sep, **kwargs)
    if _file_logger is not None and kwargs.get("file") in (None, sys.stdout):
        _file_logger.info(stamp + sep + sep.join(map(str, args)))


def write_raw(line: str):
    """Una línea tal cual, sin hora (la salida del proceso hijo, que ya trae la
    suya cuando viene de este mismo print, o un traceback que no la lleva)."""
    line = line.rstrip("\r\n")
    _orig_print(line, flush=True)
    if _file_logger is not None:
        _file_logger.info(line)
