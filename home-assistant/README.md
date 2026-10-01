# home-assistant — el servicio detect desde Home Assistant

Packages de Home Assistant y tarjetas de panel para manejar el servicio
[`detect/`](../detect/) sin pasar por Swagger: arrancar y parar el servicio y
las cámaras, ver su estado, grabar a mano, cambiar la confianza, encender el
seguimiento y mover la torreta.

Todo va por HTTP contra la API del servicio (`:8080`) y su supervisor
(`:8081`); no hace falta ninguna integración de HACS.

```
home-assistant/
  packages/
    detect_servicio.yaml    el servicio: start/stop/restart, disco, gl_keeper
    detect_huerta.yaml      cámara "huerta": PIR + grabación de clips
    detect_torreta.yaml     cámara "torreta": servos pan/tilt
  dashboards/
    detect_servicio.yaml    sección del servicio
    detect_huerta.yaml      sección de la huerta (vídeo, estado, último clip)
    detect_torreta.yaml     sección de la torreta (vídeo, cruceta, sliders)
```

Hay un package por **variante de cámara**, igual que en el firmware hay un YAML
por variante. Los `camera_id` son los de
[`cameras_config.example.json`](../detect/cameras_config.example.json):
`huerta` y `torreta`. Si tus cámaras se llaman de otra forma, ver
[Otra cámara](#otra-cámara).

---

## Instalación

### 1. Poner la IP del servicio

La IP de la máquina donde corre `supervisor.py` se pone **una sola vez**, en
`packages/detect_servicio.yaml`:

```yaml
input_text:
  detect_host:
    initial: "192.168.1.10"
```

Todo lo demás (URLs de los `rest_command`, sensores, aviso al móvil y tarjeta
del último clip) la lee de `input_text.detect_host`. Por eso los packages de
cámara **necesitan** el de servicio.

Con `initial` manda el YAML: se puede cambiar desde la UI para probar, pero al
reiniciar HA vuelve a la del fichero.

### 2. Activar los packages

Si no lo tienes ya, en `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Copia los ficheros de `packages/` que te sirvan a la carpeta `packages/` de la
configuración de HA (junto a `configuration.yaml`). El de servicio va siempre
(lleva la IP); los de cámara, los que te sirvan.

*Herramientas para desarrolladores → YAML → Comprobar configuración*, y
reinicia HA.

### 3. Añadir el vídeo

La cámara MJPEG de HA ya no se configura por YAML, así que esto va desde la UI:
*Ajustes → Dispositivos y servicios → Añadir integración → **MJPEG IP Camera***.

| Campo | Huerta | Torreta |
| --- | --- | --- |
| Nombre | `Detect huerta` | `Detect torreta` |
| URL MJPEG | `http://<ip>:8080/cameras/huerta/stream` | `http://<ip>:8080/cameras/torreta/stream` |
| URL de imagen fija | `http://<ip>:8080/cameras/huerta/snapshot` | `http://<ip>:8080/cameras/torreta/snapshot` |
| Verificar SSL | no | no |

**La imagen fija solo se valida con la cámara despierta.** Si la placa está
dormida, `/snapshot` responde 503 y el asistente dice que no puede conectar.
Déjala vacía: HA saca la imagen fija del propio stream.

Esto es lo único donde la IP va escrita a mano: la integración MJPEG no admite
plantillas. El nombre importa: las tarjetas esperan `camera.detect_huerta` y
`camera.detect_torreta`. Si HA les pone otro id, cámbialo en la tarjeta o
renombra la entidad.

Sin `?infer=` el stream sale con o sin cajas según el interruptor *Cajas en el
stream* de la tarjeta. Si quieres una cámara fija con cajas y otra en crudo,
añade dos con `?infer=true` y `?infer=false`.

### 4. Pegar las secciones

Cada fichero de `dashboards/` es una **sección** completa para una vista de tipo
*sections* (la de por defecto en los paneles nuevos): un encabezado más
tarjetas `tile`. En el panel: *Editar → Añadir sección*, y en el menú ⋮ de la
sección nueva, *Editar en YAML*: pega el contenido del fichero y guarda.

Las cámaras usan `camera_view: auto`, que muestra una imagen fija que se
refresca y abre el vídeo en vivo al pulsarla. Con `live` el stream se queda
abierto mientras el panel esté a la vista, y eso cuenta como cliente para el
servicio: mantiene la inferencia encendida.

---

## Qué crea cada package

Los ids de las entidades están en inglés (`detect_*`); las etiquetas en
castellano las ponen las tarjetas.

### `detect_servicio.yaml`

| Entidad | Qué es |
| --- | --- |
| `input_text.detect_host` | La IP del servicio, que usan todos los packages |
| `switch.detect_service` | Encender = `POST :8081/service/start`; apagar = `/service/stop` (parada manual: el supervisor no lo relanza hasta encenderlo) |
| `script.detect_service_restart` | `POST :8081/service/restart` |
| `script.detect_recordings_sweep` | Aplica la retención de clips ahora (`POST /recordings/sweep`) |
| `binary_sensor.detect_service_running` | Estado según el supervisor |
| `sensor.detect_service_uptime` | Segundos en marcha, con `pid`, `last_exit_code` y `manual_stop` como atributos |
| `sensor.detect_clips_count`, `sensor.detect_clips_total`, `sensor.detect_disk_free` | De `GET /recordings/stats` |
| `binary_sensor.detect_gl_keeper_active` | Si la ventana OpenGL anti-P5 está abierta |
| `rest_command.detect_service_start` / `_stop` / `_restart` / `detect_recordings_sweep` | Para usar en automatizaciones |

### `detect_huerta.yaml`

| Entidad | Qué es |
| --- | --- |
| `switch.detect_huerta_camera` | Arrancar / parar la cámara. Apagar es la **parada manual** de `/stop`: ni el PIR la rearranca hasta volver a encenderla |
| `switch.detect_huerta_record` | Grabar un clip a mano (con pre-roll). Necesita la grabación configurada con `POST /cameras/huerta/config/recording` |
| `switch.detect_huerta_boxes` | `default_infer`: cajas o vídeo crudo en `/stream` y `/snapshot` |
| `switch.detect_huerta_detect_always` | `always_infer`: detectar aunque nadie mire (gasta GPU) |
| `input_number.detect_huerta_confidence` | Confianza mínima de YOLO. Se sincroniza con el valor real del servicio |
| `binary_sensor.detect_huerta_running` / `_esphome` / `_recording` | Estado de la cámara, de la API nativa de la placa y de la grabación |
| `sensor.detect_huerta_fps` / `_inference` / `_corrupt_frames` / `_clients` / `_dropped_frames` / `_confidence` | Diagnóstico de `GET /cameras/huerta/status` (cada 10 s) |
| `sensor.detect_huerta_last_clip` | El último clip, con `url`, `thumbnail_url`, `labels`, `started_at`, `duration_sec`… |
| `automation.detect_huerta_aviso_de_clip_nuevo` | Notificación al móvil con vídeo y miniatura. **Viene apagada**: cambia `notify.mobile_app_tu_movil` por el tuyo y actívala |
| `rest_command.detect_huerta_*` | `start`, `stop`, `record_start` (con `note`), `record_stop`, `inference` (JSON en `body`), `stream` |

Por ejemplo, grabar cuando salte el PIR de la placa:

```yaml
automation:
  - alias: Grabar la huerta al detectar movimiento
    triggers:
      - trigger: state
        entity_id: binary_sensor.huerta_awake   # el de la integración ESPHome
        to: "on"
    actions:
      - action: rest_command.detect_huerta_record_start
        data:
          note: PIR
```

### `detect_torreta.yaml`

Lo mismo que la huerta salvo la grabación, más la torreta:

| Entidad | Qué es |
| --- | --- |
| `switch.detect_torreta_follow` | Seguimiento automático (`enabled` de `/config/servo`). Apagado, la torreta solo obedece al control manual |
| `input_number.detect_torreta_pan` / `_tilt` | Posición en unidades de servo (-1 … 1). Al soltar el slider se manda; si el seguimiento mueve la torreta, los sliders la siguen |
| `script.detect_torreta_move` | Ir a `pan`, `tilt` absolutos |
| `script.detect_torreta_step` | Paso relativo `d_pan`, `d_tilt` desde la posición actual (lo usan las flechas) |
| `script.detect_torreta_center` | A 0, 0 |
| `sensor.detect_torreta_pan` / `_tilt` | Última posición enviada, ya recortada a `pan_limit` / `tilt_limit` |
| `binary_sensor.detect_torreta_servo_service` | Si la placa publica el servicio de servos. Apagado con la placa conectada = firmware sin servos |
| `rest_command.detect_torreta_servo_move` / `_servo_config` | Para automatizaciones; `servo_config` admite cualquier campo de `/config/servo` en `body` |

Por ejemplo, cambiar la ganancia del seguimiento desde una automatización:

```yaml
- action: rest_command.detect_torreta_servo_config
  data:
    body: { gain: 0.2, deadzone: 0.06 }
```

---

## Otra cámara

1. Copia el package de su variante (`detect_huerta.yaml` si es cámara con
   PIR/grabación, `detect_torreta.yaml` si lleva servos) con otro nombre.
2. Reemplaza en el fichero el `camera_id` (`huerta` → `patio`): cambia de golpe
   las URLs, los ids de entidad, los `unique_id` y los nombres.
3. Igual con la tarjeta de `dashboards/`.
4. Añade su cámara MJPEG desde la UI como `Detect patio`.

---

## Cosas a saber

- **Sin autenticación.** El servicio no tiene ninguna, ni en `:8080` ni en
  `:8081`. Cualquiera que alcance esos puertos puede parar el servicio, mover la
  torreta o descargarse los clips. Solo en la LAN.
- **HA por https.** Si abres HA por https (Nabu Casa, proxy…), el navegador
  bloquea las URLs `http://` del servicio en la tarjeta del último clip (*mixed
  content*) y la miniatura no sale. El vídeo de la cámara MJPEG sí se ve, porque
  lo pide el servidor de HA, no el navegador.
- **Clips en negro.** Si los MP4 se descargan pero se ven en negro, el servicio
  está grabando sin H.264: `GET /recordings/capabilities`. Ver
  [`detect/docs/GRABACION.md`](../detect/docs/GRABACION.md).
- **Servicio parado = entidades `unavailable`.** Todo lo del `:8080` deja de
  responder; solo `binary_sensor.detect_service_running` y el switch del
  servicio siguen funcionando, porque van contra el supervisor.
- **Un error al arrancar HA.** Si la integración REST hace su primera lectura
  antes de que exista `input_text.detect_host`, esa petición va a
  `http://unknown:...` y deja un error en el log. En el siguiente
  `scan_interval` (10 s las cámaras, 30–300 s el resto) ya va bien.
- **Cámara dormida.** Con la placa dormida por el PIR, la cámara aparece parada
  y la torreta responde `503`: es lo esperado, no un fallo.
