# Módulo elevador de tensión TPS61088 (10A)

Ficha de referencia del módulo boost basado en el chip **TPS61088**
(Texas Instruments), **elegido como riel de 5V de los servos** de la torreta
en lugar del [MT3608](mt3608-boost-converter.md) y el
[XL6009](xl6009-boost-converter.md) (ver
[`step-up-boost-comparativa.md`](step-up-boost-comparativa.md)). A
diferencia de esos dos, hay módulos reales en venta que sí sacan el pin `EN`
a un pad — por ejemplo
[este de AliExpress](https://es.aliexpress.com/item/1005009535413093.html)
("TPS61088 Boost Module 5V/9V/12V 10A 1MHz").

## Identificación

- **Chip principal:** **TPS61088** (Texas Instruments) — convertidor
  **boost síncrono** totalmente integrado (MOSFET de potencia de 11mΩ y de
  rectificación de 13mΩ, sin diodo externo), hasta **10A de corriente de
  conmutación** ([datasheet TI, SLVSCM8A](https://www.ti.com/lit/ds/symlink/tps61088.pdf)).
- **Entrada:** 2.7-12V (encaja con la batería 1S 3.0-4.2V de este proyecto).
  **Salida:** 4.5-12.6V, ajustable por resistencias (los módulos comerciales
  suelen traer jumpers/soldaduras para 5V/9V/12V, igual que el TPS63020).
  En este proyecto se fija a **5V**: es el único paso válido para los servos,
  porque el siguiente (9V) ya supera el máximo de los MG90S (6V) y también el
  de los DS3218MG (6.8V) de [`servos-pan-tilt.md`](servos-pan-tilt.md).
- **Frecuencia de conmutación:** ajustable 200kHz-2.2MHz por resistencia
  externa (el módulo típico viene fijado a 1MHz de fábrica).
- **Modo de luz de carga:** seleccionable entre PFM (más eficiente, pin
  `MODE` flotante) y PWM forzado (`MODE` a GND) — igual filosofía que el
  MT3608 y el TPS61088, pero aquí es una elección explícita, no automática.
- **Diferencia clave frente a MT3608/XL6009:** el pin `EN` de este chip
  tiene una **resistencia de pull-down interna de 800kΩ** — es decir, si se
  deja flotando, el regulador **se apaga por defecto**, al revés que el
  XL6009 (flota a nivel alto = encendido). Esto obliga a cualquier módulo
  comercial a exponer una forma de llevar `EN` a nivel alto (jumper, botón o
  pad), porque si no el módulo ni siquiera arrancaría — es la garantía de
  que, a diferencia del XL6009, aquí sí hay un control de apagado real y
  accesible sin modificar la placa.

## Consumo en reposo (quiescent current)

Datos de la tabla "Electrical Characteristics" del datasheet TI (VIN =
3.6V, TJ = 25°C salvo indicación):

| Condición | Corriente | Nota |
| --- | --- | --- |
| Habilitado, sin carga, corriente por el pin `VIN` | **1µA típ / 3µA máx** | `VEN=2V`, sin carga |
| Habilitado, sin carga, corriente por el pin `VOUT` | **110µA típ / 250µA máx** | Con el divisor resistivo de feedback conectado a `VOUT` — sube algo el total real |
| Apagado (`EN=0V`) | **1µA típ / 3µA máx** | "Shutdown current into the VIN pin" |

**Lectura práctica:** sumando ambos términos, el consumo real habilitado y
sin carga ronda **~100-250µA** (dominado por la corriente que se pierde en
el propio divisor de feedback en `VOUT`, no por el núcleo del chip) — muy
por debajo de los 2.5-5mA del XL6009 y comparable o mejor que los 100-200µA
del MT3608 en PFM. Y en apagado (`EN=0V`) cae a **1-3µA**, prácticamente
igual de bueno que el TPS63020 (<1µA) y muy por debajo del XL6009 en
shutdown (70-100µA, y encima sin poder llegar a ese modo en el módulo
típico).

## Rol en el proyecto

Riel de **5V dedicado a los servos**, separado del riel de 3.3V que el
[TPS63020](tps63020-buck-boost.md) da a la ESP32-S3-CAM. Ambos módulos
cuelgan en paralelo de la misma batería 1S y comparten **`GND` con la
placa** — obligatorio, no opcional: la señal PWM de los servos sale de un
GPIO del ESP32 y necesita la misma referencia de masa que el riel que los
alimenta.

- **Presupuesto de corriente de entrada:** al ser boost, la corriente que
  pide a la batería es mayor que la que entrega. Dos MG90S en stall a la vez
  son ~1.4-1.8A a 5V (ver [`servos-pan-tilt.md`](servos-pan-tilt.md)); con
  ~85% de eficiencia y la celda a 3.3V eso son **~2.5-3A de entrada** en el
  peor caso. La batería y su cableado tienen que aguantarlo, no solo el
  módulo.
- **Sin necesitar el MOSFET [IRLZ44N](irlz44n-mosfet.md)**: al tener el
  módulo un pin `EN` real y funcional, un GPIO de la ESP32 puede controlar
  directamente el encendido/apagado del regulador (con las mismas
  precauciones de un pull-down si el pin quedara flotando durante el
  arranque, aunque aquí el propio chip ya trae uno de 800kΩ internamente).
- **10A de corriente de switch** deja mucho margen por encima de los picos
  de stall documentados en [`servos-pan-tilt.md`](servos-pan-tilt.md)
  (hasta ~4.2A con dos DS3218MG/DS3225MG a la vez) — a diferencia del
  MT3608 y el XL6009, cuyo "2A"/"4A" de rótulo no se sostenía en la
  práctica con módulos genéricos.
- **Coste:** más caro que un MT3608/XL6009 de trimpot (esos módulos suelen
  costar menos de 1€ en más de 1 unidad), pero sigue siendo una pieza
  barata y ampliamente disponible, no un componente exótico.

## Notas de integración

- Comprobar en la placa concreta si la salida viene fijada por jumper/pad
  (5V/9V/12V) o por potenciómetro — el listado de AliExpress citado arriba
  la anuncia como seleccionable entre 5V/9V/12V, más simple de fijar que un
  trimpot analógico como el del MT3608/XL6009.
- Verificar con multímetro qué pad concreto de la placa corresponde a `EN`
  antes de conectarlo a un GPIO — el pinout exacto puede variar entre
  clones, igual que se advierte para el [IRLZ44N](irlz44n-mosfet.md).
- Si el módulo no expone `EN` a pesar de lo anunciado (variante de placa
  distinta a la fotografiada en el anuncio), la alternativa de respaldo es
  la misma que para el XL6009: cortar la línea de entrada con un MOSFET
  externo.
- Poner un condensador electrolítico de **1000-2200µF** en la salida, lo más
  cerca posible de los servos, para amortiguar los picos cortos de stall (ya
  recomendado en [`servos-pan-tilt.md`](servos-pan-tilt.md)); no sustituye
  a dimensionar la fuente, pero evita que un golpe de corriente hunda el riel.
- Respetar el orden de apagado de [`servos-pan-tilt.md`](servos-pan-tilt.md#cortar-la-alimentación-de-los-servos-ahorro-en-reposo):
  primero dejar que ESPHome suelte la señal PWM (`auto_detach_time`), después
  bajar `EN`.

---

*Documento de referencia de hardware, generado a partir del datasheet TI
SLVSCM8A y el listado de AliExpress citado arriba. Componente aún no
comprado ni cableado en ningún YAML del proyecto.*
