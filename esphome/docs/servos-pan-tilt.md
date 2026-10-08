# Servos de la torreta pan/tilt

Ficha de referencia de los servos de la torreta de `esp32-s3-cam-servo.yaml`
(ver [`esp32-s3-cam-board.md`](esp32-s3-cam-board.md)). La torreta usa servos
de **clase 9 g como mínimo**: los servos "pico" de 3-5 g se descartaron tras
quemar dos en el pan (ver [Servos pico descartados](#servos-pico-descartados)).

## Servo montado: Miuzei MF90

El servo montado es el **Miuzei MF90**
([Amazon](https://www.amazon.es/dp/B0C38H8TBY), pack de 4): micro servo de
9 g con **engranajes metálicos**, 180°, de la clase MG90S. No hay ficha
técnica publicada del MF90 como tal; las cifras de la tabla son las de
[ServoDatabase para el Miuzei MG90S](https://servodatabase.com/servo/miuzei/mg90s),
**de la clase, no medidas sobre el MF90**.

| Servo | Peso | Dimensiones (L×An×Al) | Engranajes | Par | Tensión |
| --- | --- | --- | --- | --- | --- |
| **Miuzei MF90** (montado) | ~13.4 g | 22.8 × 12.2 × 28.5 mm | Metal | 1.8 kg·cm a 4.8V / 2.2 kg·cm a 6V | 4.8-6.0V |
| MG90S genérico | ~13.4 g | ~23 × 12.2 × 29 mm | Metal | ~1.8-2.2 kg·cm | 4.8-6.0V |
| SG90 | ~9 g | ~23 × 12.2 × 29 mm | Plástico | ~1.8 kg·cm a 4.8V | 4.8-6.0V |
| DS3218MG / DS3225MG | ~60 g | ~40 × 20 × 40 mm | Metal | ~20 / 25 kg·cm | 4.8-6.8V |

El SG90 tiene la misma huella y los mismos brazos (horns) que el MF90 y vale
como alternativa barata; con engranajes de plástico, ante un bloqueo suele
pelar dientes antes que quemar el motor, pero aguanta peor los cambios de
dirección bruscos del seguimiento. Las DS32xx son otra clase de servo, para
la torreta final con pistola de agua, donde el par importa más que el tamaño.

El par (kg·cm) es el máximo con el servo bloqueado: 1.8 kg·cm sostiene 1.8 kg
a 1 cm del eje, 0.6 kg a 3 cm. Con una reducción por engranajes, el par en la
cámara se multiplica por la relación de dientes (corona de la cámara entre
piñón del servo); el tamaño absoluto de los engranajes no cambia el par.

## Compatibilidad de tensión: los alimenta el TPS61088 a 5V fijo

En este proyecto los servos no cuelgan de una pila suelta ni del riel de la
placa: tienen su propio regulador, un [TPS61088](tps61088-boost-converter.md)
fijado a **5V**, separado del [TPS63020](tps63020-buck-boost.md) que da
3.3V a la ESP32-S3-CAM (con `GND` común entre ambos). Los servos de clase
MG90S son de 4.8-6V, no de 3.3V, así que hace falta un riel de 5V real.

| Servo | Rango de tensión | ¿Vale a 5V fijos? |
| --- | --- | --- |
| Miuzei MF90 / MG90S | 4.8-6.0V | Sí |
| SG90 | 4.8-6.0V | Sí |
| DS3218MG | 4.8-6.8V (variante "Pro": 5.0-6.8V) | Sí |

El módulo TPS61088 ofrece también 9V y 12V, pero ninguno de estos servos los
admite (MG90S máx 6V, DS3218MG máx 6.8V), así que 5V es la única opción
válida.

## El pico de consumo real: por qué los servos no comparten regulador con la cámara

**Decisión tomada:** los servos van en un riel de 5V propio con un
[TPS61088](tps61088-boost-converter.md); la ESP32-S3-CAM va a 3.3V con el
[TPS63020](tps63020-buck-boost.md). Lo que sigue es el análisis que llevó a
esa separación, referido a la idea inicial de colgar todo del TPS63020 a 5V.

Más allá de la tensión, el problema serio es de **corriente instantánea**. El
datasheet del [TPS63020](tps63020-buck-boost.md) promete 2A en modo boost,
pero en la práctica (ver el detalle con caso real citado en esa ficha) un
usuario que también boosteaba desde 1S Li-ion solo consiguió sostener
**~1.9A con la batería a 3.0V**, y pasado ese punto la tensión de salida
empieza a hundirse en vez de cortar limpiamente — así que el límite
**utilizable con margen real es más bien ~1.5A**, no los 2A de la etiqueta.
Ese presupuesto (más estrecho de lo que parece) tiene que cubrir a la vez la
ESP32-S3-CAM (picos de transmisión Wi-Fi y captura de cámara) **y** los dos
servos moviéndose o forzando contra un tope mecánico — el caso normal de un
seguimiento proporcional continuo (`detect/servo_tracker.py`), no una
rareza.

Corriente de **parada forzada (stall)** por servo — el pico que hay que
presupuestar, no la corriente en movimiento libre:

| Servo | Corriente de stall (aprox.) | 2 servos a la vez |
| --- | --- | --- |
| **SG90** | ~650-800mA; en movimiento normal ~100-250mA | ~1.3-1.6 A |
| **Miuzei MF90** (montado) | **~0.55A medidos** frenándolo a mano a 5.2V (medición propia; la tensión cae ≤0.01V, así que la fuente no la limita) | **~1.1 A** |
| MG90S genérico | ~700-900mA, según reportes de usuarios en foros de Arduino/RC ([fuente 1](https://www.kpower.com/insight_bldc/7870.html/), [fuente 2](https://forum.arduino.cc/t/how-to-power-mg90s-motors-and-arduino-nano/1001853)); en movimiento normal (no forzado) ronda 120-250mA | ~1.4-1.8 A |
| **DS3218MG** | **~2.1A a 5V** (hasta 2.9A a 6.8V, según datasheet DSSERVO) | **~4.2 A** |

A eso hay que sumarle el consumo de la propia placa: la ESP32-S3 puede dar
picos de varios cientos de mA en la entrada de 5V durante una transmisión
Wi-Fi o una captura de cámara.

Medida de conjunto con el MF90, todo alimentado por USB con un cargador de
5.19V: **placa + un servo bloqueado = 0.7A**. Con los dos bloqueados a la vez
serían ~0.15 + 2 × 0.55 ≈ **1.25A**: no cabe en un USB de PC (0.5-0.9A), sí
en un cargador de 2A.

**Conclusión práctica:**

- Con **MF90/MG90S** (o SG90), el consenso de la comunidad Arduino/RC es
  tajante: usar **una fuente separada para los servos** y no colgarlos del
  mismo riel que la lógica, precisamente porque la caída de tensión bajo
  carga causa comportamiento errático (brownouts). Con dos servos parados a
  la vez (~1.4-1.8A) ya se come casi todo el presupuesto real del TPS63020,
  dejando la ESP32-S3-CAM sin margen para sus propios picos.
- Con **DS3218MG/DS3225MG** (recomendados para la torreta final con pistola
  de agua), **dos servos parados a la vez ya superan solos el límite del
  módulo** (~4.2A frente a ~1.5A reales) — descartado compartir el
  TPS63020 con estos servos bajo cualquier escenario.
- **Recomendación general, validada tanto por el caso real del TPS63020 como
  por la experiencia de la comunidad con el MG90S:** alimentar los servos
  desde una etapa de potencia aparte, no desde el mismo riel de 5V que la
  ESP32-S3-CAM, con GND común entre ambas fuentes. Añadir un condensador
  electrolítico de valor alto (1000-2200µF) cerca de los servos ayuda a
  amortiguar picos cortos, pero no sustituye a dimensionar la fuente para la
  corriente de stall real.

## Ruido en la imagen y condensador de los servos

**Síntoma:** bandas horizontales en el vídeo mientras el PWM de los servos
está activo, que desaparecen en cuanto actúa el auto-detach. Mientras
sujeta la posición, el servo da un tirón de corriente en cada pulso (50 veces
por segundo); los 5V bajan, el bajón pasa por el AMS1117 y los XC6206 hasta
el sensor, y el rolling shutter (la cámara lee fila a fila) lo convierte en
bandas.

**Arreglo probado:** dos condensadores en paralelo entre V+ y GND de los
servos, lo más cerca posible del punto donde se separan los cables del pan y
del tilt:

- **Electrolítico de 470-1000µF**, 10V o más. Tiene polaridad: la pata larga
  (+) a V+ y la de la franja (−) a GND.
- **Cerámico de 100nF**, marcado "104", sin polaridad. Los 104 baratos pueden
  medir 40-70nF con el polímetro y valen igual.

Resultado: vídeo sin bandas.

Los saltos de dirección de ±30° en movimiento, que no registraban ni Python
ni ESPHome, tenían probablemente el mismo origen (bajones de tensión o GND
compartida que deforman el pulso que lee el servo). **Los condensadores no
bastan contra ellos:** con los MF90, servos y placa en el mismo 5V/USB y los
dos condensadores puestos, siguen saliendo tirones, sobre todo en el pan.
Solo aparecen con el PWM activo; con los servos en auto-detach, no.

Con el PWM cortado (auto-detach) pasa lo contrario: con la torreta quieta y
YOLO funcionando, el pan da a veces un giro solo de ~30° a la izquierda y
vuelve en cuanto recibe una orden. Con auto-detach a 0 no pasa: sin señal, el
servo obedece a cualquier pico espurio de la línea. Por eso el seguimiento
activo no deja soltar los servos (`set_servo_hold`, ver más abajo). El
remedio de hardware, sin probar todavía: pull-down de 10kΩ de la señal a GND
junto al servo y 220-470Ω en serie, o cortar V+ de los servos al soltarlos.

Descartado: GPIO47 a 1.8V (eso solo pasa en las S3 de la serie "V", como
N8R8V; esta placa es N16R8 y el pin va a 3.3V) y un choque de timers LEDC con
el XCLK de la cámara (ver el comentario de `output:` en
`esp32-s3-cam-servo.yaml`).

Pendiente, por este orden:

1. Con "Servo auto detach" a 0, comparar los tirones con el stream abierto y
   con el stream cerrado. Si salen solo con el stream, la causa son los picos
   de consumo.
2. Alimentar los servos con un 5V propio, uniendo la GND con la de la placa
   en un único punto.
3. Si aún quedan tirones: cable de señal corto y trenzado con su GND, una
   resistencia de 220-470Ω en serie y, en último caso, un buffer a 5V
   (74AHCT125).

## Consumo si no se corta la alimentación durante el deep sleep

Las cifras de arriba son el pico de stall — lo que hay que presupuestar para
la fuente. Pero para decidir si merece la pena cortar la alimentación de los
servos en deep sleep (ver sección siguiente), importa otro número distinto:
cuánto consumen los servos **en reposo, con alimentación puesta pero sin
moverse**.

| Servo | Idle/reposo según fabricante (banco, sin carga) |
| --- | --- |
| **Miuzei MF90 / MG90S** | ~5-6mA con la electrónica en reposo sin corregir posición; sube a ~70-90mA en cuanto corrige activamente sin carga externa ([fuente](https://www.kpower.com/insight_bldc/7870.html/)). Medida propia del conjunto con dos MF90 parados: **0.165A** por USB, casi todo de la placa con la cámara transmitiendo |
| **DS3218MG / DS3225MG** | ~4-5mA "detenido" (idle), según datasheet DSSERVO, medido en banco sin carga externa |

**El matiz importante:** esa cifra de datasheet es de banco, sin carga
externa — no es lo que va a consumir el servo sujetando de verdad el peso de
la torreta (cámara y mecanismo, y en la variante pesada también la pistola
de agua) contra la gravedad. Si el conjunto no está bien equilibrado, el
motor tiene que corregir de forma continua para no ceder, y el consumo real
de "reposo con alimentación puesta" puede acercarse al rango de movimiento
normal ya documentado arriba (120-250mA en el caso del MG90S) en vez de
quedarse en los pocos mA del datasheet — depende del equilibrio mecánico de
la torreta, no solo del servo.

**Impacto en la autonomía si no se corta la alimentación:** incluso en el
mejor caso (servos bien equilibrados, consumo cercano al idle de datasheet),
dos servos sin cortar suponen del orden de **8-10mA continuos** solo por
estar encendidos — y si tienen que corregir contra el peso de la torreta,
eso puede subir con facilidad a **decenas o unos pocos cientos de mA**. Comparado
con el consumo de deep sleep de una ESP32 (decenas de µA), la diferencia es
de **2 a 4 órdenes de magnitud**: dejar los servos alimentados domina por
completo el consumo del nodo durante el sueño, sin importar cuánto se
optimice el resto del circuito (incluido el propio regulador boost, ver
[`xl6009-boost-converter.md`](xl6009-boost-converter.md#rol-previsto-xl6009-como-riel-de-servos-cortado-por-un-irlz44n)).
Es la justificación práctica de cortar la alimentación de los servos en vez
de solo despertar/dormir el regulador.

## Cortar la alimentación de los servos (ahorro en reposo)

Además de separar el riel, se corta del todo la corriente a los servos
cuando no hay seguimiento activo. Con el [TPS61088](tps61088-boost-converter.md)
elegido esto se hace **por su pin `EN`** desde un GPIO del ESP32-S3, sin
ningún componente extra: con `EN` bajo el regulador queda en 1-3µA y los
servos sin tensión.

El esquema con **IRLZ44N** que sigue (el mismo MOSFET usado para el motor de
la [pistola de agua](bambulab-zc005-water-gun.md), como interruptor de bajo
lado en el retorno a GND del servo) queda como **respaldo** por si el módulo
TPS61088 que llegue no expone `EN` en un pad accesible. Las precauciones de
orden de apagado del final de esta sección aplican igual en ambos casos.

![Esquema: corte de alimentación de los servos con IRLZ44N](img/servo-power-switch-mosfet.svg)

*Esquema generado con IA; no verificado en banco.*

Puntos clave del esquema:

- **Diodo flyback** entre V+ y GND del servo (mismo criterio que en el
  motor de la pistola: dentro del servo hay un motor DC, sigue siendo carga
  inductiva).
- **Resistencia de gate** (~220Ω) entre el GPIO y la puerta del MOSFET, y
  **pull-down** (~10kΩ) de la puerta a GND — el pull-down es lo que evita
  que el servo reciba corriente sin querer si el GPIO queda flotando durante
  el arranque del ESP32.
- **Orden de apagado importante** (vale igual para el `EN` del TPS61088):
  primero dejar que ESPHome corte la señal PWM (`auto_detach_time: 2s`, ya
  presente en `esp32-s3-cam-servo.yaml`), y solo después bajar el GPIO de
  corte. Cortar la alimentación mientras el pin de señal todavía manda PWM
  puede alimentar el servo a medias a través de sus diodos de protección
  internos.

## Qué necesita el proyecto a nivel de firmware

En `esp32-s3-cam-servo.yaml` los servos se controlan por PWM estándar a
50Hz vía el componente `servo:` de ESPHome (pulso ~1-2ms, rango -1.0..1.0
mapeado por `servo.write`) — **cualquiera de los servos de arriba sirve
igual a nivel de firmware**, es una cuestión puramente mecánica (tamaño del
soporte) y de la fuente de alimentación.

El movimiento no lo hace el `transition_length` de ESPHome (va a 0), que
arranca y para a velocidad constante, de golpe. Lo hace un `interval` de
20 ms que, por eje, lleva el servo hacia el objetivo en dos etapas:

```
mid += clamp(target - mid, ±vmax·dt)    vmax = 2 / "Servo transition"
pos += (mid - pos) · k                  k = 1 - exp(-dt / "Servo smoothing")
```

La primera es un limitador de velocidad (lo que hacía la transición de
antes); la segunda es la fórmula clásica de animación `x += (xfinal - x) / vel`,
que frena suave. Juntas, la velocidad del servo nunca salta: ni al arrancar,
ni al frenar, ni cuando llega una orden nueva a mitad de camino. Python sigue
mandando solo el objetivo.

Ajustes, sin compilar, con los number de configuración (desde Home Assistant o
desde `POST /cameras/{id}/config/servo` del servicio):

- **"Servo transition"**: velocidad máxima, como tiempo de recorrido completo
  (-1 a +1). 0 = sin límite.
- **"Servo smoothing"**: suavizado del arranque y la frenada (`tau`, hace ~95%
  del camino en 3 `tau`). 0 = movimiento lineal, el de antes. Tiene que quedar
  bastante por debajo del `min_interval_sec` del seguimiento (0.2 s): el
  suavizado retrasa la torreta y, si se acerca al intervalo entre órdenes, el
  lazo oscila. Por defecto 0.03 s.
- **"Servo auto detach"**: segundos quieto tras los que se corta el PWM. El
  interval deja de escribir al llegar, así que el auto-detach sigue
  funcionando. Mientras el seguimiento está activo, haya objetivo o no, no se
  aplica: el servicio Python llama a la acción `set_servo_hold`
  (`hold: true`), que pone el auto-detach a 0 sin tocar el number, y la repite
  cada 3 s. Con `hold: false` (seguimiento apagado), o a los 10 s sin
  refresco, vuelve el valor del number. El motivo, en "Ruido en la imagen y
  condensador de los servos".

Los valores del bloque `servo:` del YAML son solo los de arranque. El servicio
Python lee los tres number para mostrarlos en `/status`.

## Notas de integración

- El YAML ya avisa (comentario "OJO CON EL TIMER LEDC") de que los servos van
  en los canales LEDC 4 y 6 para no chocar con el reloj de la cámara — eso no
  cambia con ningún servo.
- Si se pasa a un servo de mayor par (DS3218/DS3225), revisar la
  alimentación: tiran de más corriente en el arranque/parada (~2.1A de stall
  cada uno), pero siguen cabiendo en el riel del
  [TPS61088](tps61088-boost-converter.md) a 5V — solo hay que revisar que la
  batería aguante la corriente de entrada correspondiente.

## Servos pico descartados

![Servos pico descartados: Blue Arrow D03012 arriba, Arced D531BB abajo](img/servo.png)

Los primeros servos de la torreta fueron servos "pico" de 3-4 g que había de
otro proyecto. Con dos **Arced D531BB** (0.51 kg·cm a 4.8V, ~210mA de stall
medido) en el pan, **se dañaron el motor y los engranajes de los dos**. El par
no bastaba frente al arrastre del cable USB al girar el pan: el servo se
quedaba forzando y calentándose. Por eso el mínimo de la torreta es la clase
9 g, con unas 3.5 veces más par.

---

*Documento generado con IA; revisar los valores antes de montar.*
