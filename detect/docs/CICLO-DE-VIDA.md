# Ciclo de vida de los objetos de `detect/main.py`

Quién crea cada instancia, cuánto vive, quién la destruye y qué recursos
suelta al morir. Complemento de [`ARQUITECTURA.md`](ARQUITECTURA.md), que
explica *qué hace* cada clase; esto explica *cuándo existe*.

El caso que más importa es el ciclo de sueño del ESP32: la placa está dormida
casi todo el tiempo y despierta por el PIR, así que las sesiones de cámara se
crean, paran y rearrancan constantemente. Si algo no se cerrara bien, el
servicio acumularía hilos, sockets y event loops hasta reventar.

Hay un test de regresión que cubre todo esto sin necesidad de la placa:

```powershell
cd detect
venv\Scripts\python.exe test\test_cierre.py
```

---

## 1. Mapa de instancias

| Objeto | Cuántos | Lo crea | Lo destruye | Vive |
| --- | --- | --- | --- | --- |
| `GlobalConfig` (`GLOBAL_CONFIG`) | 1 | Import del módulo | Nunca | Todo el proceso |
| Modelos `YOLO` (`_loaded_models`) | 1 por `modelo+device` | `get_model()`, perezoso | **Nunca** (a propósito) | Todo el proceso |
| `CameraSession` | 1 por cámara registrada | `register_camera()` | `DELETE /cameras/{id}` o apagado | Entre alta y baja |
| `CameraConfig` | 1 por sesión | Pydantic al registrar | Con su sesión | Igual que la sesión |
| `EsphomeController` | 1 por sesión **con** `noise_psk` | `CameraSession.__init__` | `CameraSession.shutdown()` | Igual que la sesión |
| Hilos lector + proceso | 2 por **generación** | `CameraSession.start()` | `stop()` + fin natural | Un ciclo despierto |
| `requests.Response` | 1 por conexión del lector | `_read_loop` | `finally` del lector, o `stop()` | Una conexión al stream |
| Generador MJPEG | 1 por cliente HTTP | `mjpeg_generator()` | `finally` al cerrar el cliente | Una respuesta HTTP |

Punto clave: **una `CameraSession` sobrevive a los ciclos de sueño**. Lo que
nace y muere en cada ciclo son los *hilos* y la *conexión HTTP*, no la sesión
ni su `EsphomeController`.

---

## 2. Nivel proceso

### `GLOBAL_CONFIG`

Instancia única creada al importar el módulo. Solo se muta desde
`POST /config/keepalive`. No tiene cierre.

### Cache de modelos YOLO

`_loaded_models` no se vacía nunca, ni al borrar una cámara. Es deliberado:
cargar y precalentar un modelo cuesta segundos, y varias cámaras suelen
compartirlo. Consecuencia a tener presente: **la VRAM que ocupa un modelo no
se recupera al hacer `DELETE` de la última cámara que lo usaba**. Si algún día
molesta, el sitio para liberarlo es `remove_camera`, contando antes cuántas
sesiones vivas siguen usando esa clave.

### `lifespan`

- **Arranque:** `load_cameras_from_disk()` reconstruye las sesiones guardadas
  en `cameras_config.json`. Se crean paradas; las que tienen `noise_psk`
  levantan ya su `EsphomeController`, que empieza a intentar conectar.
- **Apagado:** `session.shutdown()` de todas **en paralelo**
  (`asyncio.gather`), cada una en un hilo (`asyncio.to_thread`) y con
  `timeout=5s`. Va en un hilo porque `shutdown()` hace `join()` y bloquear el
  event loop impedía a uvicorn cerrar las conexiones de clientes a tiempo.

---

## 3. `CameraSession`

### Nacimiento

`register_camera()` → `CameraSession(cfg)`. El constructor **no arranca
hilos**: solo prepara estado y, si hay `noise_psk`, crea el
`EsphomeController` (que sí arranca su hilo inmediatamente).

### Generaciones de hilos

Cada `start()` crea una **generación** nueva: objetos nuevos de `_stop_event`,
`_raw_queue` y `_fps_window`, más dos hilos nuevos (`read-<id>` y
`yolo-<id>`).

No se reciclan a propósito. Si `start()` hiciera `_stop_event.clear()` sobre
el mismo objeto, un hilo viejo que todavía no se hubiera enterado del `stop()`
anterior (bloqueado en `queue.get()`) despertaría, vería el evento ya limpio y
seguiría corriendo: dos generaciones vivas a la vez pisándose `_fps_window` y
publicando frames alternos. Con objetos nuevos, el hilo viejo sigue mirando
*su* evento —que sigue en `set()`— y se muere solo mientras la generación
nueva arranca limpia.

Por eso `_read_loop` y `_process_loop` copian `_stop_event` y `_raw_queue` a
variables locales en su primera línea: son la referencia a *su* generación.

```mermaid
stateDiagram-v2
    [*] --> Registrada : register_camera
    Registrada --> Corriendo : start, generacion N
    Corriendo --> Registrada : stop, la generacion N muere sola
    Registrada --> Corriendo : start, generacion N+1 con objetos nuevos
    Registrada --> [*] : DELETE o apagado, shutdown
    note right of Registrada
        La sesion y su EsphomeController
        sobreviven entre ciclos de sueno.
        Solo mueren los hilos.
    end note
```

### Muerte

Solo por `shutdown()`, desde `DELETE /cameras/{id}` o desde `lifespan`. Suelta,
en este orden:

1. `stop(explicit=True)` — señal de parada + cierre forzado de la conexión HTTP.
2. `esphome.shutdown(timeout=1.5)` — primero, para que deje de reintentar
   contra la placa y cierre su socket mientras los hilos de vídeo terminan.
3. `join()` de los dos hilos con un **deadline compartido de 2,5 s**.
4. `_cond.notify_all()` — despierta a cualquier generador que siguiera esperando.

---

## 4. `EsphomeController`

Nace con la sesión y **vive todo lo que viva la sesión**, incluso mientras la
placa está dormida: es precisamente quien tiene que enterarse de que ha
despertado.

Su hilo (`esphome-<ip>`) y su event loop propio se crean en el constructor y
solo se cierran en `shutdown()`.

### Reconexión mientras la placa duerme

Cuando la placa se duerme, cierra la conexión de la API nativa → `_on_stop` →
`_disconnected_event.set()` → el bucle reintenta. Cada intento se acota a
`connect_attempt_timeout_sec` (1 s); si falla, espera **hasta 30 s
pasivamente** o hasta que `notify_awake()` lo despierte antes. Cero polling
mientras está conectado.

Esto **no es una fuga**: es un único hilo dormido en un `await`, sin sockets
abiertos entre intentos.

### Cierre

`shutdown()` levanta los dos eventos que hacen salir a `_reconnect_loop` por
su propio pie y **espera al hilo**; el hilo cierra el socket y el loop en
`_close_loop()`, dentro de sí mismo.

> **Por qué así.** Antes `shutdown()` programaba `disconnect()` y acto seguido
> hacía `loop.stop()` desde fuera. El loop paraba antes de que la corrutina
> llegara a ejecutarse: el socket con la placa quedaba abierto hasta que moría
> el proceso —un descriptor filtrado por cada `DELETE` de cámara— y
> `run_until_complete` reventaba con *"Event loop stopped before Future
> completed"*, matando el hilo con un traceback por consola.

`shutdown()` es idempotente, y `notify_awake()` / `move_servo()` comprueban
`_closed` antes de tocar el loop, así que llamarlos después no lanza nada.

---

## 5. El ciclo de sueño, paso a paso

### La placa se duerme

```mermaid
sequenceDiagram
    autonumber
    participant ESP as ESP32 (PIR agotado)
    participant EC as EsphomeController
    participant S as CameraSession
    participant RL as hilo read-huerta
    participant PL as hilo yolo-huerta
    participant C as Clientes MJPEG

    ESP->>EC: estado = off (API nativa)
    EC->>S: _on_esphome_state(False)
    S->>S: stop(explicit=True)
    Note over S: explicit_start=False, _stop_event.set()
    S->>RL: _force_close_response, shutdown del socket
    Note over RL: recv se rompe al instante, 0.1 ms
    RL->>RL: except, stop_event set, break y cierra la respuesta
    S->>C: _cond.notify_all()
    C->>S: remove_client en el finally del generador
    PL->>PL: queue vacia, ve stop_event, sale
    ESP--xEC: deep sleep, cae la conexion de la API
    EC->>EC: _on_stop, reintenta cada 30s o al recibir aviso
```

El punto crítico es el paso 4. `stop()` lo llama **el event loop del
`EsphomeController`**, así que no puede bloquearse: si lo hace, ese hilo deja
de procesar estados y la reconexión se retrasa. Ver §6.

### La placa despierta

```mermaid
sequenceDiagram
    autonumber
    participant ESP as ESP32 (PIR)
    participant API as POST /esphome/awake
    participant EC as EsphomeController
    participant S as CameraSession

    ESP->>API: wifi.on_connect, en cuanto tiene IP
    API->>EC: notify_awake, _wake_event.set()
    Note over EC: cancela la espera de 30s y conecta ya
    EC->>ESP: connect + subscribe_states
    ESP->>EC: estado = on
    EC->>S: _on_esphome_state(True)
    S->>S: start(explicit=True), generacion nueva
    Note over S: _stop_event, _raw_queue y _fps_window nuevos
    S->>ESP: GET stream_url, hilo lector
```

### Rearranque si la placa se duerme sin avisar

Si la placa cae sin llegar a publicar `estado=off` (corte de WiFi, batería), el
lector no puede reconectar. Cuando además el `EsphomeController` está
desconectado, el lector **deja de insistir** en vez de machacar una IP muerta
cada segundo.

Para que eso no deje la cámara muerta para siempre, `_on_esphome_state`
deduplica repeticiones solo si la sesión ya está como debería:

```python
if value == self._last_esphome_state and value == self.is_running:
    return
```

Así, un `estado=on` repetido al reconectar **sí** rearranca la sesión si la
lectura estaba parada. Antes se ignoraba por ser el mismo valor.

---

## 6. Cierre de la conexión HTTP con la cámara

Es el recurso más delicado: el hilo lector está bloqueado dentro de
`iter_content()` y hay que desbloquearlo desde otro hilo.

`resp.close()` **no vale por sí solo**. En Windows, cerrar el objeto fichero no
interrumpe el `recv()` en curso: medido en el test, `stop()` tardaba **12,23
segundos** en volver. Y como `stop()` lo llama el event loop del
`EsphomeController` y los endpoints `async`, ese bloqueo congelaba la API
entera durante esos 12 segundos, justo en el momento del apagado.

`_force_close_response()` hace `sock.shutdown(SHUT_RDWR)` sobre el socket
subyacente antes del `close()`. Eso rompe el `recv()` al instante (**0,1 ms**
medidos) y baja el `stop()` completo a **0,01 s**. El lector se despierta con
un `ChunkedEncodingError` que su propio `except` interpreta como parada.

Como llegar al socket depende de las tripas de `urllib3`, se prueban varias
rutas y, si ninguna funciona, el `close()` se delega a un hilo desechable: en
el peor caso el que se bloquea es ese y no quien pidió la parada.

### El `except` del lector es amplio a propósito

Cerrar la respuesta desde otro hilo hace saltar cosas distintas según dónde
pille a `urllib3`: a veces un `RequestException`, pero también
`ValueError("I/O operation on closed file")` o un `AttributeError` sobre un
socket ya puesto a `None`. Cazando solo `RequestException`, esos casos mataban
el hilo con un traceback en vez de salir por el camino limpio. Ahora es
`except Exception` y lo primero que mira es `stop_event`: si la parada estaba
pedida, la excepción es la consecuencia, no la causa.

---

## 7. Presupuesto de tiempo al cerrar

uvicorn arranca con `--timeout-graceful-shutdown 5`, y `lifespan` da 5 s por
sesión. `CameraSession.shutdown()` se ajusta a eso:

| Paso | Tope |
| --- | --- |
| `stop()` + cierre forzado del socket | ~0,01 s |
| `esphome.shutdown()` (incluye `disconnect`) | 1,5 s |
| `join()` de los hilos lector y proceso | 2,5 s (deadline compartido) |
| **Total** | **~4 s** |

Latencia real de salida de cada hilo:

| Hilo | Sale en | Por qué |
| --- | --- | --- |
| Proceso (`yolo-*`) | ≤ 50 ms con keep-alive, ≤ 1 s sin él | Es el timeout de su `queue.get()` |
| Lector, dentro de `iter_content` | ~0,01 s | Le rompen el socket |
| Lector, dentro de `requests.get` | ≤ 3 s | Connect timeout contra una placa dormida |

Los tres hilos son **daemon**: si alguno se pasa del plazo se deja dicho en el
log, pero no impide que el proceso muera.

---

## 8. Recursos por cliente HTTP

Un generador MJPEG vive lo que dure la respuesta. `add_client()` al entrar,
`remove_client()` en un `finally`, así que un cliente que se va sin avisar
también descuenta.

Al irse el último cliente, `_maybe_autostop()` para la sesión **solo si** no
hay `explicit_start`. Con ESPHome, `explicit_start` lo pone el `estado=on`, así
que cerrar el navegador no apaga una cámara que el hardware dice que está
despierta.

Al revés, cuando la placa se duerme `stop()` corta **todas** las respuestas
MJPEG abiertas: los clientes ven terminar el stream y les toca reconectar.
Home Assistant lo hace solo.

No hay una cola por cliente: todos leen el mismo `_latest_*_jpeg`, así que un
cliente lento no acumula memoria, solo se salta frames.

---

## 9. Resumen de qué se suelta y qué no

**Se suelta correctamente:**

- Socket del stream MJPEG (`:8080`) — forzado al parar, y en el `finally` del lector.
- Socket de la API nativa (`:6053`) — `disconnect()` dentro del propio loop del controller.
- Event loop y hilo del `EsphomeController` — `loop.close()` en `_close_loop()`.
- Hilos lector y de proceso — salen por su propio `stop_event`.
- Contadores de clientes — `finally` en cada generador.

**No se suelta, a propósito:**

- Modelos YOLO en `_loaded_models` y su VRAM (cache compartida; ver §2).
- El `EsphomeController` mientras la placa duerme (es quien la espera).

**A vigilar si se toca el código:**

- Nada que se llame desde `_on_esphome_state` puede bloquear: corre en el
  event loop del controller.
- Nada bloqueante en un endpoint `async` sin `asyncio.to_thread` —
  `remove_camera` y `snapshot_camera` ya lo tuvieron y congelaban todos los
  streams a la vez.
- `asyncio.CancelledError` hereda de `BaseException`, no de `Exception`: un
  `except Exception` no la caza y se escapa del hilo.
