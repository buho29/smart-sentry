"""Frontera entre el pipeline de vídeo y lo que se hace con las detecciones.

El proyecto tiene varias variantes de placa (cámara sola, cámara+PIR,
cámara+servos para seguimiento, cámara+PIR+servos para la pistola). Si la lógica
de cada una viviera dentro de `CameraSession._process_loop`, ese bucle acabaría
siendo un amasijo de condicionales por variante, y encima intestable: para
probar el seguimiento de servos harían falta una GPU y un ESP32 delante.

Así que `_process_loop` solo produce una lista de `Detection` y se la pasa a los
consumidores registrados en la sesión. Cada funcionalidad de hardware es un
`DetectionConsumer` en su propio módulo, y añadir una variante nueva no toca el
bucle de inferencia.

`Detection` no depende de ultralytics a propósito: los consumidores se prueban
construyendo estos objetos a mano, sin modelo, sin GPU y sin placa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Detection:
    """Una caja de un frame ya traducida a números planos.

    Las coordenadas van en PÍXELES del frame original (ultralytics ya las
    reescala desde imgsz), con el origen arriba a la izquierda.
    """

    x1: float
    y1: float
    x2: float
    y2: float
    cls: int
    label: str
    conf: float
    # ID de ByteTrack. None mientras el track no está confirmado: quien siga un
    # objetivo tiene que saber distinguir "sin ID" de un ID cualquiera, porque
    # esas cajas cambian de identidad entre frames.
    track_id: Optional[int] = None

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


@runtime_checkable
class DetectionConsumer(Protocol):
    """Algo que reacciona a las detecciones de una cámara.

    Todos los métodos se llaman desde el hilo de proceso de la sesión
    (`yolo-<camera_id>`), uno detrás de otro y sin lock: **no deben bloquear**.
    Para hablar con la placa hay que usar algo asíncrono como
    `EsphomeController.llamar_servicio`, que solo encola la orden en otro hilo;
    una llamada de red síncrona aquí frenaría el pipeline de vídeo entero.
    """

    def on_detections(self, dets: list[Detection], width: int, height: int) -> None:
        """Un frame nuevo con sus detecciones (la lista puede estar vacía)."""

    def on_idle(self) -> None:
        """No ha llegado ningún frame en el último ciclo de espera.

        Sirve para soltar el objetivo o volver a reposo cuando la cámara deja de
        dar imagen, en vez de quedarse apuntando a un fantasma.
        """

    def wants_inference(self) -> bool:
        """Si hay que ejecutar YOLO aunque no haya ningún cliente HTTP mirando.

        Sin esto el seguimiento se moriría al cerrar el navegador: `want_infer`
        solo cuenta clientes de stream.
        """

    def status(self) -> dict:
        """Estado para exponer en /status. Depurar esto sin leer logs."""

    def shutdown(self) -> None:
        """Se llama al parar la sesión, con la conexión a la placa todavía viva."""
