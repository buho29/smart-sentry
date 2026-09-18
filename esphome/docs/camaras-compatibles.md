# Cámaras compatibles con la ESP32-S3-CAM: reguladores y consumo

Ficha que responde a dos preguntas sobre la placa
[UICPAL ESP32-S3-CAM](esp32-s3-cam-board.md): qué sensores de cámara se
pueden enchufar en su conector FPC de 24 pines, y qué tensiones y corriente
entregan los reguladores de cámara que lleva la propia placa. Aplica a los
tres YAML con cámara (`esp32-s3-cam.yaml`, `esp32-s3-cam-servo.yaml` y
`huerta.yaml`).

## Qué entrega la placa al sensor

Según el esquemático del vendedor (ver
[`img/esp32/`](img/esp32/S7161d80f606f4004a9e87bbd5bff2e5cf.webp)), el
socket de cámara recibe tres tensiones y dos señales de control fijas:

| Rail / señal      | Origen en la placa                                  | Valor       | Para qué pin del sensor     |
| ----------------- | --------------------------------------------------- | ----------- | --------------------------- |
| `VCC2.8V`         | **U5 — XC6206-2.8**, entrada desde `VCC3.3V`        | **2.8V**    | AVDD (analógico) y DOVDD (I/O) |
| `VCC1.2V`         | **U4 — XC6206-1.2**, entrada desde `VCC3.3V`        | **1.2V**    | DVDD (núcleo digital)       |
| `OV_PWDN`         | **R11 (1K) a GND**, sin GPIO                        | siempre bajo | PWDN — la cámara **nunca se apaga por hardware** |
| `OV_RESET`        | pull-up a `VCC2.8V`, sin GPIO                       | siempre alto | RESET — solo reset por software (SCCB) |
| XCLK              | GPIO15 (`external_clock` en los YAML)               | **13MHz**   | reloj maestro del sensor    |

Datos del regulador **XC6206** (Torex,
[datasheet](https://www.torex-usa.com/products/voltage-regulators/low-iq/xc6206/)):

- **Corriente máxima:** 200mA (la versión de 1.2V garantiza algo menos por el
  poco margen entre entrada y salida; sigue sobrando para cualquier sensor de
  la tabla).
- **Dropout:** ~250mV a 100mA → con 3.3V de entrada quedan 0.5V de margen
  para el rail de 2.8V. Si el `3V3` que entra por el
  [TPS63020](tps63020-buck-boost.md) cae por debajo de ~3.05V, el rail de
  2.8V empieza a desregular.
- **Consumo propio (Iq):** ~1µA cada uno → despreciable frente al sensor.
- Son reguladores **lineales**: la corriente que piden al nodo `VCC3.3V` es
  la misma que entregan al sensor; la diferencia de tensión se disipa como
  calor. Un sensor que consuma 140mW en sus rails cuesta ~50mA en el nodo
  3.3V (≈165mW), no 140mW.

El punto que decide la compatibilidad es el **rail de 1.2V**: la placa está
pensada para sensores cuyo núcleo digital funciona a 1.2-1.3V (familia
OV2640). Los sensores modernos tienen núcleo a 1.5V o 1.8V y llevan un
**regulador interno** que genera ese núcleo a partir de DOVDD, así que no
necesitan el 1.2V de la placa — pero que el módulo FPC concreto deje ese pin
sin conectar o lo cablee al núcleo depende del fabricante del módulo, no del
sensor.

## Tabla de compatibilidad

Sensores que el driver `espressif/esp32-camera` v2.1.7 (el que usa el
componente `esp32_camera` de ESPHome) sabe inicializar y que existen en
formato FPC 24 pines para ESP32-CAM. Todos se autodetectan por SCCB; no hay
que nombrar el sensor en el YAML.

| Sensor     | Resolución máx.        | JPEG en sensor | AVDD          | DOVDD          | DVDD (núcleo)                                | Encaja con la placa                       | Veredicto |
| ---------- | ---------------------- | -------------- | ------------- | -------------- | -------------------------------------------- | ----------------------------------------- | --------- |
| **OV2640** | 2MP, UXGA 1600×1200 @15fps | **Sí**     | 2.5-3.0V      | 1.7-3.3V       | **1.3V ±5%, externo** (sin regulador interno) | 2.8V ✔, 2.8V ✔, **1.2V en el límite inferior** del rango 1.235-1.365V — es lo que monta el vendedor y funciona | **Compatible (el actual)** |
| OV3660     | 3MP, QXGA 2048×1536 @15fps | Sí         | 2.6-3.0V      | 1.8V / 2.8V    | 1.5V ±5%, **regulador interno**              | 2.8V ✔, 2.8V ✔, 1.2V no sirve como núcleo → el módulo tira del regulador interno desde 2.8V | **No recomendado — probado en la placa: el módulo se calienta en exceso** (ver abajo) |
| OV5640     | 5MP, QSXGA 2592×1944 @15fps | Sí        | 2.6-3.0V      | 1.7-3.0V (1.8V recomendado) | 1.5V, regulador interno **solo recomendado con DOVDD 1.8V**; con I/O a 2.8V OmniVision pide 1.5V externo | 2.8V ✔, 2.8V ✔, la placa no da 1.5V → el módulo tira del regulador interno desde 2.8V | **No recomendado — probado en la placa: 72°C en el módulo** (ver abajo) |

El resto de sensores del driver (OV7670, OV7725, GC2145, NT99141 y demás) no
comprimen JPEG en el sensor, así que la ESP32-S3 tendría que comprimir cada
frame por software (`frame2jpg`), justo lo contrario de lo que busca el nodo
a batería; quedan fuera de la tabla.

### Cómo leer la tabla

- **JPEG en sensor** es la columna que más pesa para este proyecto: el
  `esp32_camera_web_server` sirve MJPEG directamente con los frames que ya
  vienen comprimidos.
- El OV2640 va con DVDD en el extremo bajo de su rango (1.2V frente a 1.3V
  nominal). Es la práctica estándar en todas las placas ESP32-CAM (la
  AI-Thinker original hace lo mismo) y funciona, pero explica por qué algunos
  módulos OV2640 dan problemas cuando el `3V3` baja y el XC6206-1.2 pierde
  regulación.
- **OV3660 y OV5640 se han probado en esta placa y se calientan en exceso.**
  El driver los reconoce y dan imagen, pero el módulo OV5640 llega a **72°C**
  al aire y mantiene la lente a **55°C** dentro de la carcasa impresa. La
  explicación encaja con la columna DVDD: como la placa no da 1.5V, el módulo
  genera el núcleo con el regulador interno del sensor a partir de 2.8V, un
  regulador lineal dentro del propio chip disipando (2.8−1.5V)×I_núcleo, algo
  que OmniVision solo recomienda con DOVDD a 1.8V. Sumado a que ambos
  consumen el doble o el triple que el OV2640, el calor se queda en un
  encapsulado de pocos milímetros sin disipación. Inviable en una carcasa
  cerrada al sol. No es un defecto de este módulo concreto: el vídeo
  ["Overheating Issue? OV5640 Temperature Test with ESP32CAM board"](https://www.youtube.com/watch?v=yzWiicnv-f8)
  y varios hilos de la comunidad ([Arduino: "OV5640 solution for overheating"](https://forum.arduino.cc/t/ov5640-solution-for-overheating/1420017),
  [Arduino: "purple haze / very warm board"](https://forum.arduino.cc/t/esp32-cam-with-ov5640-overheating-purple-haze-over-image-and-very-warm-board-camera/1216720),
  [esp32-camera #670](https://github.com/espressif/esp32-camera/issues/670))
  reproducen lo mismo en cualquier placa tipo ESP32-CAM con LDO de núcleo a
  1.2/1.3V.

  ![Térmica del módulo OV5640 al aire: 72.3°C en el sensor](img/temp%20cam%20ov5640.jpg)

  ![Térmica del OV5640 montado en la carcasa: 54.8°C en la lente](img/temp%20cam%20ov5640%201.jpg)

### Por qué se calientan y cómo se arreglaría

Causa confirmada por la comunidad (hilos citados arriba): el OV3660/OV5640
tiene un regulador lineal interno que genera el núcleo de **1.5V a partir de
DOVDD**. Con DOVDD a 1.8V el salto es pequeño y OmniVision lo da por bueno;
con los **2.8V** que entrega esta placa el sensor disipa dentro del
encapsulado **~75mW a bajo framerate y hasta ~127mW a resolución alta**, y
el 1.2V del U4 (XC6206-1.2) no le sirve de nada porque está fuera de su rango.

El arreglo documentado tiene dos partes, y no está probado en este proyecto:

1. **Hardware:** sustituir **U4** por un LDO de **1.5V**. El hilo de Arduino
   usa un NCP115AMX150TCG; en esta placa el cambio más directo es un
   **XC6206P152MR** (misma familia y mismo SOT-23 que el U4 actual, así que
   es un cambio pin a pin).
2. **Software:** después de inicializar la cámara, activar el **bit 3 del
   registro 0x3031 (SC PWC)** del OV5640 para que desconecte su regulador
   interno y use el 1.5V externo:

   ```c
   sensor_t *s = esp_camera_sensor_get();
   s->set_reg(s, 0x3031, 0x08, 0x08);
   ```

   El ajuste **no sobrevive a un reset del sensor**, así que hay que hacerlo
   en cada arranque. En ESPHome iría en una lambda `on_boot` (prioridad
   posterior a la inicialización de `esp32_camera`); el proyecto ya mantiene
   una copia local parcheada del componente en `components/esp32_camera/`,
   que sería el sitio natural si se quisiera integrar.

Con el arreglo, la disipación se traslada del sensor al LDO externo (un
usuario reporta 62°C en un OV3660 sin ventilación tras la modificación —
mejor, pero sigue siendo caliente). **Trade-off:** con U4 a 1.5V el
**OV2640 queda fuera de especificación** (DVDD 1.3V ±5%), así que la placa
modificada pasa a ser "solo OV3660/OV5640"; no es reversible sin volver a
cambiar el LDO.

## Consumo de cada sensor

Valores de datasheet o de módulos comerciales; los mA en el nodo 3.3V son
estimación propia asumiendo reguladores lineales y que todo el consumo pasa
por ellos.

| Sensor  | Activo (datasheet)                                     | ≈ en el nodo 3.3V | Standby por SCCB | Fuente |
| ------- | ------------------------------------------------------ | ----------------- | ---------------- | ------ |
| OV2640  | **125mW** (UXGA YUV 15fps) / **140mW** (UXGA JPEG 15fps) | ~45-60mA         | **600µA**        | [Datasheet OV2640 v1.6](https://www.uctronics.com/download/cam_module/OV2640DS.pdf) |
| OV3660  | **98mA** (la hoja lo da en corriente, no en potencia)   | ~100mA            | **20µA**         | [OV3660 product brief](https://pdf.datasheet.technology/01014492/ovt.com/OV03660-A51A.txt) |
| OV5640  | **~140mA** (dato de módulo comercial; el datasheet pone "TBD") | ~140mA     | **20µA**         | [e-con Systems OV5640](https://www.e-consystems.com/ov5640-5mp-mipi-camera-module.asp) |

Estos consumos son los del sensor solo. Los picos de ~0.5A que cita la ficha
del [TPS63020](tps63020-buck-boost.md) son a nivel de placa completa (Wi-Fi +
captura + PSRAM), y la cámara es la parte pequeña.

### Lo que importa para el nodo a batería (`huerta.yaml`)

- Como **PWDN está fijado a GND por R11**, en deep sleep el sensor sigue
  alimentado por los dos XC6206. El firmware no manda al sensor a standby
  antes de dormir (el componente `esp32_camera` no lo hace), así que el
  consumo real en reposo está entre el standby de datasheet (600µA para
  OV2640) y algo más si el sensor se queda con XCLK parado pero sin orden
  de standby. **Hay que medirlo**; es un candidato a ser el mayor
  consumidor en reposo del nodo por delante de los 25µA del TPS63020 (ver
  [step-up-boost-comparativa.md](step-up-boost-comparativa.md)).
- Si se confirma que pesa, las opciones son: (a) mandar el sensor a standby
  por SCCB en `on_shutdown`/antes de `deep_sleep.enter`; (b) modificación de
  hardware: quitar R11 y llevar `OV_PWDN` a un GPIO libre para que ESPHome lo
  controle con `power_down_pin`; (c) cortar el 3.3V de entrada de los dos
  XC6206 con un interruptor de alta (load switch) gobernado por GPIO. El
  standby de 20µA de OV3660/OV5640 no es una salida: en activo se calientan
  demasiado en esta placa (ver arriba).

## Recomendación

- **Seguir con el OV2640**: es el único que encaja con los tres rails de la
  placa sin regulador interno trabajando desde 2.8V, tiene JPEG en sensor y
  su UXGA ya es el tope de `resolution:` que usan los YAML.
- **Descartar OV3660 y OV5640 en esta placa tal cual**: probados y se
  calientan en exceso (72°C el OV5640). Si algún día hace falta más
  resolución, la vía es la modificación descrita arriba (U4 a 1.5V + registro
  0x3031 en cada arranque), asumiendo que esa placa deja de aceptar OV2640;
  no basta con cambiar el módulo.

## Notas de integración en los YAML

- No hay que declarar el modelo; `esp32_camera` autodetecta por SCCB.
- Cambiar de sensor obliga a revisar `resolution:` (los enumerados de ESPHome
  llegan hasta QSXGA) y `external_clock.frequency` (13MHz actual; ESPHome
  admite 8-20MHz).
- Las entidades de exposición/ganancia/balance de blancos del YAML son
  genéricas del driver y no dependen del sensor.
- El pin `power_down_pin` de ESPHome no sirve en esta placa mientras R11 esté
  montada: PWDN no llega a ningún GPIO.

---

*Documento de referencia de hardware, generado a partir del esquemático del
vendedor de la placa, el Kconfig de `espressif/esp32-camera` v2.1.7 (sensores
habilitados por defecto), el datasheet OV2640 v1.6, el product brief del
OV3660, notas de aplicación del OV5640 y el datasheet XC6206 de Torex. La causa del calentamiento y el arreglo (LDO de
1.5V + registro 0x3031) salen del vídeo
["Overheating Issue? OV5640 Temperature Test with ESP32CAM board"](https://www.youtube.com/watch?v=yzWiicnv-f8)
y de los hilos de Arduino citados en el texto. OV2640, OV3660 y OV5640 se han
probado en la placa; las fotos térmicas del OV5640 están en [`img/`](img/).
El arreglo no se ha probado.*

*Documento generado con IA; revisar los valores antes de montar.*
