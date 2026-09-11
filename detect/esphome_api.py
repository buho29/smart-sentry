"""Conexion con la API nativa de ESPHome (puerto 6053) de una placa.

Autonomo: no sabe nada de camaras, ni de YOLO, ni del hardware concreto que
lleve la placa. Solo hace tres cosas: vigilar una entidad, llamar a los
servicios que el YAML publique, y cerrarse bien.

Lo del hardware es a proposito. Aqui llego a haber un move_servo() cableado a
`set_servo_position`, y con la siguiente variante (el rele de disparo de la
pistola) habria hecho falta un fire_relay(), y luego otro, y otro. Los
`api: services:` de ESPHome ya son genericos —nombre y argumentos con
nombre—, asi que cada variante llama al suyo con call_service() y este
modulo no se entera. Quien decide CUANDO llamar es un DetectionConsumer (ver
detections.py), no esto.
"""

import asyncio
import threading
import time
from typing import Callable, Optional

from aioesphomeapi import APIClient

from log import print


# ---------------------------------------------------------------------------
# EsphomeController: conexión persistente a la API nativa de ESPHome
# (puerto 6053) de una placa, en su propio hilo con loop de asyncio propio,
# para poder llamarla/leerla desde hilos síncronos (como CameraSession)
# sin bloquearlos.
#
# Cubre dos usos:
#   - call_service(name, **args): llama a cualquiera de los
#     `api: services:` que publique el YAML, sin bloquear a quien llama. Si la
#     placa no publica ese servicio devuelve False en vez de fallar, que es lo
#     que permite que un firmware sin ese hardware no rompa nada.
#   - Vigila una entidad concreta (por object_id, p.ej. "awake", que en
#     el YAML de la placa es un binary_sensor) y llama a on_state_value(bool)
#     cada vez que cambia. Así una CameraSession puede arrancar/parar en
#     función del estado real del hardware (PIR + deep sleep) en vez de solo
#     por clientes HTTP.
# ---------------------------------------------------------------------------

class EsphomeController:
    """Conexión a la API nativa de ESPHome de una placa, manejando APIClient
    directamente (sin ReconnectLogic): un solo dispositivo, latencia
    controlada por nosotros, sin depender de mDNS ni de atributos privados
    de la librería.

    Reconexión: al desconectarse, reintenta una vez. Si falla, espera
    pasivamente hasta 'safety_retry_sec' (red de seguridad, por si el
    webhook falla) O hasta que notify_awake() la despierte antes -- eso es
    lo que llama el endpoint que golpea el 'on_connect' del propio ESP32.

    connect_attempt_timeout_sec: cada intento de connect() individual se
    acota a esto (en vez de fiarnos del timeout por defecto de la librería,
    que ronda los ~10s). Sin esto, si notify_awake() llega mientras ya hay
    un intento fallido "en vuelo" contra la placa todavía dormida, la señal
    se queda esperando a que ESE intento viejo agote su propio timeout
    antes de poder arrancar el intento bueno -- confirmado en logs reales
    (~10s de retraso entre WiFi conectado y el Accept en el ESP).
    """

    def __init__(
        self,
        address: str,
        noise_psk: Optional[str] = None,
        port: int = 6053,
        watch_entity_object_id: Optional[str] = None,
        on_state_value: Optional[Callable[[object], None]] = None,
        safety_retry_sec: float = 30.0,
        connect_attempt_timeout_sec: float = 1.0,
    ):
        self.address = address
        self.port = port
        self.noise_psk = noise_psk
        self.watch_entity_object_id = watch_entity_object_id
        self.on_state_value = on_state_value
        self.safety_retry_sec = safety_retry_sec
        self.connect_attempt_timeout_sec = connect_attempt_timeout_sec

        self._client: Optional[APIClient] = None
        # nombre -> UserService, tal cual los publica el YAML de la placa.
        self._services: dict = {}
        self._watch_key: Optional[int] = None
        self._connected = threading.Event()
        self._stopping = False
        # Se marca cuando el hilo ya ha cerrado el socket y el event loop.
        # Sirve para que shutdown() sea idempotente y para que notify_awake()/
        # call_service() no intenten programar nada en un loop ya cerrado.
        self._closed = threading.Event()

        # Creados aquí, se usan dentro del loop propio de este controller.
        self._disconnected_event = asyncio.Event()
        self._disconnected_event.set()  # empezamos "desconectados" -> primer intento inmediato
        self._wake_event = asyncio.Event()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"esphome-{address}"
        )
        self._thread.start()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._reconnect_loop())
        finally:
            # El cierre se hace DENTRO de este hilo, que es el único sitio
            # donde el loop sigue vivo y se puede esperar de verdad a que el
            # socket con la placa (puerto 6053) se cierre.
            #
            # Antes shutdown() hacía loop.stop() desde fuera justo después de
            # programar disconnect(): el loop paraba antes de que esa corrutina
            # llegara a ejecutarse, así que el socket quedaba abierto hasta que
            # moría el proceso (un descriptor filtrado por cada DELETE de
            # cámara), y run_until_complete de arriba reventaba con
            # "Event loop stopped before Future completed".
            self._close_loop()

    def _close_loop(self):
        """Cierre ordenado del cliente, las tareas pendientes y el loop.
        Solo se llama desde el propio hilo del controller."""
        try:
            if self._client is not None:
                self._loop.run_until_complete(
                    asyncio.wait_for(self._client.disconnect(), timeout=1.0)
                )
        except Exception:
            # Si la placa ya se ha dormido no hay nadie al otro lado y el
            # disconnect puede fallar o agotar el timeout: da igual, lo que
            # importa es que el socket local quede cerrado igualmente.
            pass
        try:
            pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        except Exception:
            pass
        self._loop.close()
        self._closed.set()

    async def _reconnect_loop(self):
        self._client = APIClient(self.address, self.port, password="", noise_psk=self.noise_psk)
        while not self._stopping:
            await self._disconnected_event.wait()
            if self._stopping:
                break

            self._wake_event.clear()
            connect_task = asyncio.ensure_future(self._try_connect_once())
            wake_task = asyncio.ensure_future(self._wake_event.wait())
            done, _pending = await asyncio.wait(
                {connect_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if connect_task in done:
                wake_task.cancel()
                try:
                    ok = connect_task.result()
                except Exception:
                    ok = False
                if ok:
                    self._disconnected_event.clear()
                    # nos quedamos aquí, sin hacer nada, hasta que _on_stop
                    # vuelva a marcar _disconnected_event -- cero polling
                    # mientras está conectado.
                    continue
                # falló de verdad (agotó connect_attempt_timeout_sec u otro
                # error) -> esperamos hasta el siguiente aviso o hasta la
                # red de seguridad.
                self._wake_event.clear()
                # Ojo con este clear(): si shutdown() acaba de levantar
                # _wake_event, se lo lleva por delante y nos quedaríamos aquí
                # los safety_retry_sec (30s) enteros pese a estar parando. Como
                # shutdown() pone _stopping ANTES de programar el set, mirarlo
                # justo después del clear cierra la ventana por los dos lados:
                # si el clear ganó, esto lo ve; si no, el set despierta al wait.
                if self._stopping:
                    break
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self.safety_retry_sec)
                except asyncio.TimeoutError:
                    pass
                # seguimos en el while: _disconnected_event sigue set -> reintenta
            else:
                # nos avisaron MIENTRAS el intento anterior seguía en curso
                # (p.ej. contra la placa todavía dormida): lo cancelamos en
                # vez de esperar a que agote su propio timeout, y dejamos el
                # cliente en un estado limpio antes de reintentar ya.
                connect_task.cancel()
                try:
                    await connect_task
                except (asyncio.CancelledError, Exception):
                    # CancelledError hereda de BaseException, NO de Exception:
                    # sin nombrarla aquí se escapaba de este except, subía por
                    # run_until_complete y mataba el hilo del controller con un
                    # traceback cada vez que shutdown() o notify_awake()
                    # cancelaban un intento de conexión en vuelo.
                    pass
                try:
                    # acotado: contra una placa dormida un disconnect sin
                    # límite podría pasarse del presupuesto de shutdown()
                    await asyncio.wait_for(self._client.disconnect(), timeout=1.0)
                except (asyncio.CancelledError, Exception):
                    pass
                # seguimos en el while: _disconnected_event sigue set -> reintenta ya, sin esperar

    async def _try_connect_once(self) -> bool:
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(
                self._client.connect(login=True, on_stop=self._on_stop),
                timeout=self.connect_attempt_timeout_sec,
            )
        except Exception as e:
            print(f"EsphomeController[{self.address}]: fallo al conectar: {e!r}")
            return False

        entities, services = await self._client.list_entities_services()
        self._services = {s.name: s for s in services}

        self._watch_key = None
        if self.watch_entity_object_id:
            for e in entities:
                if getattr(e, "object_id", None) == self.watch_entity_object_id:
                    self._watch_key = e.key
                    break
            if self._watch_key is None:
                print(f"EsphomeController[{self.address}]: aviso, no encontré "
                      f"la entidad '{self.watch_entity_object_id}'")

        self._client.subscribe_states(self._on_state)
        self._connected.set()
        print(f"EsphomeController[{self.address}]: conectado en "
              f"{(time.perf_counter() - t0) * 1000:.0f}ms "
              f"({len(entities)} entidades, servicios: "
              f"{', '.join(self._services) or 'ninguno'})")
        return True

    async def _on_stop(self, expected_disconnect: bool = False):
        # Callback de APIClient.connect(on_stop=...) -- se llama al perderse
        # la conexión. La firma exacta (con/sin expected_disconnect) varía
        # entre versiones de aioesphomeapi; con valor por defecto aceptamos
        # ambas sin romper si algún día cambia otra vez.
        self._connected.clear()
        if not self._stopping:
            print(f"EsphomeController[{self.address}]: desconectado "
                  f"(esperado={expected_disconnect}), reintentando...")
        self._disconnected_event.set()

    def notify_awake(self):
        """Llamar cuando algo externo (el webhook del propio ESP32 al
        conectar WiFi) nos indica que la placa puede estar lista, para
        saltarnos la espera de safety_retry_sec y reintentar ya."""
        if self._closed.is_set():
            return
        try:
            self._loop.call_soon_threadsafe(self._wake_event.set)
        except RuntimeError:
            pass  # el loop se cerró entre el check y esta llamada

    def _on_state(self, state):
        # Llamado en el hilo/loop propio de este controller.
        if self._watch_key is not None and getattr(state, "key", None) == self._watch_key:
            # 'awake' es un binary_sensor: state.state es un bool. Antes de la
            # primera publicación, aioesphomeapi marca missing_state=True y
            # state.state no significa nada -> lo tratamos como "sin valor".
            if getattr(state, "missing_state", False):
                return
            value = getattr(state, "state", None)
            print(f"EsphomeController[{self.address}]: '{self.watch_entity_object_id}' -> {value!r}")
            if self.on_state_value is not None:
                try:
                    self.on_state_value(value)
                except Exception as e:
                    print(f"EsphomeController[{self.address}]: error en on_state_value callback:", repr(e))

    # -- servicios de la placa -------------------------------------------

    @property
    def services(self) -> tuple[str, ...]:
        """Los `api: services:` que publica el YAML de esta placa."""
        return tuple(self._services)

    def has_service(self, name: str) -> bool:
        """Si la placa publica ese servicio.

        Es lo que de verdad se quiere saber cuando algo "no se mueve": llamar a
        un servicio que el firmware no expone falla en silencio, y desde fuera
        es indistinguible de un error de puntería o de un relé mal cableado.
        """
        return name in self._services

    def call_service(self, name: str, /, **args) -> bool:
        """Encola una llamada a un servicio de la placa. Devuelve si se encoló.

        **No bloquea**: solo deja la orden en el event loop propio de este
        controller. Esa es la razón de que se pueda llamar desde el hilo de
        proceso de una cámara (ver `DetectionConsumer` en detections.py) sin
        frenar el pipeline de vídeo.

        `name` va posicional-only para que un argumento del servicio que se
        llamara `name` no choque con él.
        """
        if not self._connected.is_set() or self._closed.is_set():
            return False
        service = self._services.get(name)
        if service is None:
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self._execute_service(name, service, args), self._loop)
        except RuntimeError:
            return False  # el loop se cerró entre el check y esta llamada
        return True

    async def _execute_service(self, name: str, service, args: dict):
        try:
            self._client.execute_service(service, args)
        except Exception as e:
            print(f"EsphomeController[{self.address}]: error llamando a "
                  f"'{name}':", repr(e))

    # -- apagado ---------------------------------------------------------

    def shutdown(self, timeout: float = 1.5):
        """Para el hilo y cierra la conexión con la placa. Idempotente.

        No para el loop a la fuerza: solo levanta los dos eventos que hacen
        salir a _reconnect_loop por su propio pie, y espera a que el hilo
        cierre el socket y el loop en _close_loop(). Así el disconnect()
        siempre llega a ejecutarse dentro del loop, que es donde asyncio
        puede esperarlo.
        """
        if self._closed.is_set():
            return
        self._stopping = True
        try:
            self._loop.call_soon_threadsafe(self._disconnected_event.set)
            self._loop.call_soon_threadsafe(self._wake_event.set)
        except RuntimeError:
            return  # el loop ya estaba cerrado
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            print(f"EsphomeController[{self.address}]: el hilo no terminó en "
                  f"{timeout}s (es daemon, no bloquea el cierre del proceso)")
