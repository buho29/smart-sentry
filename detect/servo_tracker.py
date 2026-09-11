"""Seguimiento con servos: mueve una torreta pan/tilt para centrar un objetivo.

Es el primer consumidor de detecciones (ver `detections.py`): recibe las cajas
de cada frame desde el hilo de proceso de la cámara y traduce "el objetivo está
descentrado" en órdenes de servo hacia la placa.

El contrato con el firmware es un servicio de la API nativa de ESPHome llamado
`set_servo_position` con dos variables float, `pan` y `tilt`, **en el rango
-1.0 a 1.0** (lo que espera `servo.write` de ESPHome). No se manejan grados en
ninguna capa: no sabemos ni la geometría del montaje ni el campo de visión de la
lente, así que hablar de ángulos absolutos sería inventarse una precisión que no
existe.

Por ese mismo motivo el control es proporcional y relativo: en vez de calcular
"el objetivo está a 12 grados, apunta ahí", se corrige un poco en la dirección
del error en cada frame y se deja que el lazo cerrado converja. Sale estable con
ganancia baja y no necesita calibración.
"""

from __future__ import annotations

import time
from typing import Optional

from pydantic import BaseModel

from detections import Detection


class ServoConfig(BaseModel):
    """Ajustes del seguimiento, persistidos en cameras_config.json.

    Una cámara sin servos simplemente no trae esta sección (`servo: None`), y
    entonces la sesión no crea el consumidor.
    """

    enabled: bool = True
    # Cómo se llama en el YAML de esta placa el servicio que mueve la torreta.
    # Va en la config y no cableado en el código para que una variante pueda
    # nombrarlo distinto sin tocar nada.
    service: str = "set_servo_position"
    # Fracción del error que se corrige en cada envío. Más alto = más rápido
    # pero con riesgo de sobrepasar el objetivo y oscilar.
    gain: float = 0.25
    # Error normalizado por debajo del cual se considera centrado y NO se mueve.
    # Sin zona muerta el servo tiembla sin parar persiguiendo el ruido de la
    # caja, que baila unos píxeles entre frames aunque el objeto esté quieto.
    deadzone: float = 0.06
    # Tope de frecuencia de envío. A 25 fps, mandar un servicio por frame satura
    # la API nativa sin ganar nada: el servo tarda más en llegar a la posición
    # que en llegar el frame siguiente.
    min_interval_sec: float = 0.08
    # Según cómo quede montado cada servo, corregir "a la derecha" puede ser
    # sumar o restar. Se ajusta aquí en vez de recompilando el firmware.
    invert_pan: bool = False
    invert_tilt: bool = False
    # Tiempo sin ver el objetivo antes de soltarlo y poder enganchar otro.
    lost_target_sec: float = 1.5
    # Posición de reposo, usada al arrancar y al apagar.
    home_pan: float = 0.0
    home_tilt: float = 0.0
    # Al perder el objetivo, ¿volver a reposo? Por defecto no: en una torreta
    # suele interesar más quedarse mirando donde se perdió, que es por donde
    # probablemente reaparezca.
    return_home_on_lost: bool = False


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


class ServoTracker:
    """Consumidor de detecciones que centra el objetivo con dos servos.

    Elegir objetivo se hace por **bloqueo de track ID**: se engancha uno y se
    sigue mientras siga visible, en vez de recalcular el "mejor" cada frame.
    Con dos objetos en escena, elegir cada frame haría que la torreta saltara
    entre ellos en cuanto uno se acercara un píxel más al centro.
    """

    def __init__(self, cfg: ServoConfig, esphome, camera_id: str = ""):
        # `esphome` es un EsphomeController, pero aquí solo se le piden
        # llamar_servicio() y tiene_servicio(): cualquier doble sirve para los
        # tests.
        self.cfg = cfg
        self._esphome = esphome
        self._camera_id = camera_id

        self.pan = cfg.home_pan
        self.tilt = cfg.home_tilt
        self.target_id: Optional[int] = None
        self._last_seen: float = 0.0
        self._last_send: float = 0.0
        self._sends = 0

    # -- elección de objetivo ------------------------------------------------

    def _elegir_objetivo(self, dets: list[Detection], width: int, height: int) -> Optional[Detection]:
        """Mantiene el objetivo bloqueado si sigue ahí; si no, engancha otro.

        Solo se consideran cajas con track_id: sin ID confirmado, ByteTrack no
        garantiza que la caja de este frame sea el mismo objeto que la del
        anterior, y bloquearse a eso sería bloquearse a nada.
        """
        con_id = [d for d in dets if d.track_id is not None]

        if self.target_id is not None:
            actual = next((d for d in con_id if d.track_id == self.target_id), None)
            if actual is not None:
                self._last_seen = time.monotonic()
                return actual
            # Perdido, pero puede ser una oclusión de un par de frames: se le da
            # margen antes de soltarlo, para no cambiar de objetivo por un
            # parpadeo del detector.
            if time.monotonic() - self._last_seen < self.cfg.lost_target_sec:
                return None
            self._soltar_objetivo()

        if not con_id:
            return None

        # Objetivo nuevo: el más cercano al centro, que es el que menos hay que
        # mover la torreta para atender.
        cx0, cy0 = width / 2, height / 2
        nuevo = min(con_id, key=lambda d: (d.cx - cx0) ** 2 + (d.cy - cy0) ** 2)
        self.target_id = nuevo.track_id
        self._last_seen = time.monotonic()
        print(f"[{self._camera_id}] servo: objetivo #{self.target_id} ({nuevo.label})")
        return nuevo

    def _soltar_objetivo(self):
        if self.target_id is None:
            return
        print(f"[{self._camera_id}] servo: objetivo #{self.target_id} perdido")
        self.target_id = None
        if self.cfg.return_home_on_lost:
            self._enviar(self.cfg.home_pan, self.cfg.home_tilt, forzar=True)

    # -- envío ---------------------------------------------------------------

    def _enviar(self, pan: float, tilt: float, forzar: bool = False) -> bool:
        ahora = time.monotonic()
        if not forzar and (ahora - self._last_send) < self.cfg.min_interval_sec:
            return False
        self.pan = _clamp(pan)
        self.tilt = _clamp(tilt)
        self._last_send = ahora
        self._sends += 1
        self._esphome.llamar_servicio(self.cfg.service, pan=self.pan, tilt=self.tilt)
        return True

    def move_to(self, pan: float, tilt: float):
        """Control manual (endpoint /servo): salta el rate limit a propósito.

        Quien mueve la torreta a mano quiere que se mueva ya, y además así se
        puede verificar el hardware sin esperar a que haya detecciones.
        """
        self._enviar(pan, tilt, forzar=True)

    # -- interfaz DetectionConsumer -----------------------------------------

    def on_detections(self, dets: list[Detection], width: int, height: int) -> None:
        if not self.cfg.enabled or width <= 0 or height <= 0:
            return

        objetivo = self._elegir_objetivo(dets, width, height)
        if objetivo is None:
            return

        # Error normalizado a [-1, 1]: independiente de la resolución, así que
        # cambiar imgsz o la resolución de la cámara no descalibra la ganancia.
        ex = (objetivo.cx - width / 2) / (width / 2)
        ey = (objetivo.cy - height / 2) / (height / 2)

        if abs(ex) < self.cfg.deadzone and abs(ey) < self.cfg.deadzone:
            return  # ya está centrado: no gastar movimiento ni ancho de banda

        paso_pan = self.cfg.gain * ex
        paso_tilt = self.cfg.gain * ey
        if self.cfg.invert_pan:
            paso_pan = -paso_pan
        if self.cfg.invert_tilt:
            paso_tilt = -paso_tilt

        # El signo por defecto asume una torreta que mira hacia delante: si el
        # objetivo aparece a la derecha de la imagen, la cámara tiene que girar
        # hacia ese lado, lo que en el montaje de referencia es restar pan.
        self._enviar(self.pan - paso_pan, self.tilt + paso_tilt)

    def on_idle(self) -> None:
        # Sin frames no hay detecciones, y el objetivo caduca igual que si la
        # cámara siguiera dando imagen sin él: si no, al volver la señal la
        # torreta seguiría enganchada a un ID que ByteTrack ya no reconoce.
        if self.target_id is not None and \
                time.monotonic() - self._last_seen >= self.cfg.lost_target_sec:
            self._soltar_objetivo()

    def wants_inference(self) -> bool:
        return self.cfg.enabled

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "pan": round(self.pan, 3),
            "tilt": round(self.tilt, 3),
            "target_id": self.target_id,
            "sends": self._sends,
            # Lo que de verdad se quiere saber cuando "no se mueve": si la placa
            # llegó a publicar el servicio. Sin esto la llamada falla en
            # silencio y no hay forma de distinguirlo de un error de puntería.
            "service": self.cfg.service,
            "servo_service": self._esphome.tiene_servicio(self.cfg.service),
        }

    def shutdown(self) -> None:
        # A reposo antes de que se cierre la conexión: si no, el servo se queda
        # donde estuviera apuntando hasta el próximo arranque.
        try:
            self._enviar(self.cfg.home_pan, self.cfg.home_tilt, forzar=True)
        except Exception as e:
            print(f"[{self._camera_id}] servo: fallo al volver a reposo:", repr(e))
