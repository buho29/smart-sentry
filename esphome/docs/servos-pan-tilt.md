# Servos de la torreta pan/tilt

Ficha de referencia de los dos servos que aparecen en
[`img/servo.png`](img/servo.png) — stock antiguo de otro proyecto, candidatos
a montar la torreta de `esp32-s3-cam-servo.yaml` (ver
[`esp32-s3-cam-board.md`](esp32-s3-cam-board.md)). Ambos están identificados
en [ServoDatabase](https://servodatabase.com/) y descatalogados hace años;
esta ficha recoge sus specs reales y propone alternativas actuales.

![Dos servos: Blue Arrow D03012 arriba, Arced D531BB abajo](img/servo.png)

## Identificación confirmada

| | Servo superior | Servo inferior |
| --- | --- | --- |
| Modelo | **Blue Arrow D03012** | **Arced D531BB** |
| Ficha | [servodatabase.com/servo/blue-arrow/d03012](https://servodatabase.com/servo/blue-arrow/d03012) | [servodatabase.com/servo/arced/d531bb](https://servodatabase.com/servo/arced/d531bb) |
| Peso | **3.2 g** | **3.9 g** |
| Dimensiones (L×An×Al) | 19.6 × 8.4 × 21.8 mm | 19.0 × 7.9 × 17.5 mm |
| Motor | Coreless | Coreless |
| Engranajes | Plástico | No especificado en la ficha |
| Control | **Digital** | **Digital** |
| Conector | JST-ZH (miniatura, no el JR/Futaba estándar de 2.54mm) | No especificado |
| Torque | 0.30 kg·cm a 4.2V / 0.25 kg·cm a 3.3V | 0.51 kg·cm a 4.8V |
| Velocidad | 0.08s/60° a 4.2V | 0.09s/60° a 4.8V |
| Estado | **Descatalogado** | **Descatalogado** |

Dato relevante que corrige la ficha anterior: **la etiqueta del Arced dice
"Analog Micro Servo", pero ServoDatabase lo cataloga como digital** — es
habitual que el rotulado comercial de estos servos de bazar no sea fiable;
para las specs reales toca fiarse de la base de datos, no de la caja.

Lo más importante para elegir sustituto: **ambos son servos "pico"**, mucho
más pequeños y ligeros que un micro servo estándar tipo SG90/MG90S (que
pesan ~9g y miden ~23×12×29mm). El conector JST-ZH tampoco es el estándar de
3 pines a 2.54mm que llevan la mayoría de servos RC — al montar alternativas
modernas con conector JR/Futaba estándar, hay que adaptar el cableado en la
placa (ver pines usados en `esp32-s3-cam-servo.yaml`, que solo necesitan
señal + alimentación + GND por PWM, sin depender del tipo de conector físico).

## ¿Las alternativas recomendadas antes tienen las mismas medidas/peso?

**No.** SG90, MG90S y DS3218/DS3225 (las recomendadas en la versión anterior
de este documento) son de una categoría de tamaño distinta:

| Servo | Peso | Dimensiones (L×An×Al) | Categoría |
| --- | --- | --- | --- |
| Blue Arrow D03012 (original) | 3.2 g | 19.6 × 8.4 × 21.8 mm | Pico |
| Arced D531BB (original) | 3.9 g | 19.0 × 7.9 × 17.5 mm | Pico |
| SG90 / MG90S | ~9 g | ~23 × 12.2 × 29 mm | Micro estándar |
| DS3218MG / DS3225MG | ~60 g | ~40 × 20 × 40 mm | Estándar de alto par |

Es decir, SG90/MG90S pesan **el triple** y son notablemente más grandes; las
DS32xx son directamente otra clase de servo (para la torreta final con
pistola de agua, donde el par importa más que el tamaño). Si lo que se busca
es un **reemplazo del mismo tamaño/peso** que el D03012/D531BB originales
(por ejemplo, para no rediseñar el soporte mecánico existente), hacen falta
servos "pico", no micro estándar:

### Reemplazos del mismo tamaño/peso (pico, ~3-6g)

- **E-max ES9051** — 4.3g, 19×8×23mm, digital, coreless, engranajes de
  plástico, 0.8kg·cm de par, conector JR estándar. Es prácticamente el mismo
  tamaño y peso que el D03012 original, y se compra hoy sin problema (Amazon,
  tiendas de aeromodelismo). [Ficha](https://servodatabase.com/servo/e-max/es9051).
- **JX PDI-1102HB** — 4.3g, plástico, digital, coreless, sub-micro; otra
  opción equivalente igual de disponible en tiendas de aeromodelismo.
- **Power HD DSM44** — algo mayor (5.8g, 20×8.7×27mm) pero sigue en la misma
  familia de tamaño; a cambio lleva **engranajes de aluminio** en vez de
  plástico, más par (1.2-1.6kg·cm) y más velocidad (0.07-0.09s/60°) — mejor
  opción si se quiere algo más duradero sin saltar a la categoría de 9g.
  [Specs](https://www.pololu.com/product/2142/specs).

Estos sí son sustitutos "drop-in" en tamaño y peso del D03012/D531BB
originales, con la ventaja de ser piezas actuales y no descatalogadas.

## Compatibilidad de tensión: los alimenta el TPS63020 a 5V fijo

En este proyecto los servos no cuelgan de una pila suelta: los alimenta el
mismo [TPS63020](tps63020-buck-boost.md) que da los 5V a la placa
ESP32-S3-CAM (puenteado a la salida `5V`, ver esa ficha). Eso fija la tensión
de los servos en **5V constantes**, y ahí el D03012 original tiene un
problema:

| Servo | Tensión máxima según ficha | ¿Vale a 5V fijos? |
| --- | --- | --- |
| Blue Arrow D03012 (original) | **4.2V** | **No** — 5V lo sobrepasa casi un 20%, fuera de su rango documentado (3.3-4.2V) |
| Arced D531BB (original) | Caracterizado a 4.8V (máximo no confirmado en la ficha) | Dudoso — 5V está justo por encima del único valor documentado |
| E-max ES9051 | 4.0-5.5V | Sí, con margen |
| Power HD DSM44 | 4.8-6.0V | Sí |
| MG90S | 4.8-6.0V | Sí |
| DS3218MG | 4.8-6.8V (variante "Pro": 5.0-6.8V) | Sí |

Es decir: si el plan es alimentar los servos desde la misma salida de 5V que
usa la cámara, el **D03012 original no es apto** (quedaría sobrealimentado de
forma permanente, no solo en un pico), y el D531BB es cuando menos incierto.
Todas las alternativas modernas listadas arriba (ES9051, DSM44, MG90S,
DS3218MG) sí admiten 5V dentro de su rango normal, así que no añaden ninguna
restricción extra sobre la tensión ya fijada por el TPS63020.

Si en algún momento se quisiera aprovechar los servos originales pese a
todo, el TPS63020 tiene un pad de salida a **4.2V** (ver
[tps63020-buck-boost.md](tps63020-buck-boost.md)) que encajaría con el
D03012 — pero entonces esa misma salida ya no serviría para alimentar la
ESP32-S3-CAM (que necesita 5V), y haría falta un segundo regulador o módulo
para separar ambas tensiones.

### Si en cambio se prefiere subir de categoría (más robustez, menos precisión de tamaño)

Sigue siendo válido lo recomendado antes, pero asumiendo que implica rehacer
el soporte mecánico porque el tamaño no es compatible:

- **MG90S** — micro servo digital estándar (~9g), engranajes metálicos, el
  más común y documentado hoy; solo tiene sentido si de todos modos se va a
  ajustar/imprimir un soporte nuevo.
- **DS3218MG / DS3225MG** — para la torreta final a la intemperie con la
  pistola de agua (ver README, "water-tower-defense"), donde el par y la
  resistencia a salpicaduras pesan más que mantener el tamaño original.

## El pico de consumo real: dos servos + cámara en el mismo TPS63020

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
| Blue Arrow D03012 / Arced D531BB (pico, sin dato oficial) | No publicada; los pico de esta clase suelen rondar 300-500mA | ~0.6-1.0 A |
| E-max ES9051 / DSM44 (pico/sub-micro modernos) | No publicada; comparable a la clase anterior, ~300-500mA | ~0.6-1.0 A |
| **MG90S** | **~700-900mA medidos**, según reportes de usuarios en foros de Arduino/RC ([fuente 1](https://www.kpower.com/insight_bldc/7870.html/), [fuente 2](https://forum.arduino.cc/t/how-to-power-mg90s-motors-and-arduino-nano/1001853)); en movimiento normal (no forzado) ronda 120-250mA | **~1.4-1.8 A** |
| **DS3218MG** | **~2.1A a 5V** (hasta 2.9A a 6.8V, según datasheet DSSERVO) | **~4.2 A** |

A eso hay que sumarle el consumo de la propia placa: la ESP32-S3 puede dar
picos de varios cientos de mA en la entrada de 5V durante una transmisión
Wi-Fi o una captura de cámara.

**Conclusión práctica:**

- Con los servos **pico** originales o sus reemplazos modernos (ES9051,
  DSM44), el pico combinado (servos + placa) se queda razonablemente por
  debajo del límite real de ~1.5A — es la opción más segura para compartir
  el mismo regulador que la cámara.
- Con **MG90S**, el consenso de la comunidad Arduino/RC es tajante: usar
  **una fuente separada para los servos** y no colgarlos del mismo riel que
  la lógica, precisamente porque la caída de tensión bajo carga causa
  comportamiento errático (brownouts). Con dos MG90S parados a la vez
  (~1.4-1.8A) ya se come casi todo el presupuesto real del TPS63020, dejando
  la ESP32-S3-CAM sin margen para sus propios picos — mejor no compartir
  el mismo módulo con este servo tampoco, aunque el datasheet en teoría lo
  permitiera.
- Con **DS3218MG/DS3225MG** (recomendados para la torreta final con pistola
  de agua), **dos servos parados a la vez ya superan solos el límite del
  módulo** (~4.2A frente a ~1.5A reales) — descartado compartir el
  TPS63020 con estos servos bajo cualquier escenario.
- **Recomendación general, validada tanto por el caso real del TPS63020 como
  por la experiencia de la comunidad con el MG90S:** alimentar los servos
  (cualquiera que no sea de clase pico) desde una etapa de potencia aparte,
  no desde el mismo riel de 5V que la ESP32-S3-CAM, con GND común entre
  ambas fuentes. Añadir un condensador electrolítico de valor alto
  (1000-2200µF) cerca de los servos ayuda a amortiguar picos cortos, pero no
  sustituye a dimensionar la fuente para la corriente de stall real.

## Cortar la alimentación de los servos con un MOSFET (ahorro en reposo)

Además de separar el riel, se puede cortar del todo la corriente a los
servos cuando no hay seguimiento activo, con el mismo **IRLZ44N** ya usado
para el motor de la [pistola de agua](bambulab-zc005-water-gun.md): un
interruptor de bajo lado en el retorno a GND del servo, gobernado por un
GPIO del ESP32-S3.

![Esquema: corte de alimentación de los servos con IRLZ44N](img/servo-power-switch-mosfet.svg)

Puntos clave del esquema:

- **Diodo flyback** entre V+ y GND del servo (mismo criterio que en el
  motor de la pistola: dentro del servo hay un motor DC, sigue siendo carga
  inductiva).
- **Resistencia de gate** (~220Ω) entre el GPIO y la puerta del MOSFET, y
  **pull-down** (~10kΩ) de la puerta a GND — el pull-down es lo que evita
  que el servo reciba corriente sin querer si el GPIO queda flotando durante
  el arranque del ESP32.
- **Orden de apagado importante:** primero dejar que ESPHome corte la señal
  PWM (`auto_detach_time: 2s`, ya presente en `esp32-s3-cam-servo.yaml`), y
  solo después bajar el GPIO de este interruptor. Cortar la alimentación
  mientras el pin de señal todavía manda PWM puede alimentar el servo a
  medias a través de sus diodos de protección internos.

## Qué necesita el proyecto a nivel de firmware

En `esp32-s3-cam-servo.yaml` los servos se controlan por PWM estándar a
50Hz vía el componente `servo:` de ESPHome (pulso ~1-2ms, rango -1.0..1.0
mapeado por `servo.write`) — **cualquiera de las opciones de arriba sirve
igual a nivel de firmware**, es una cuestión puramente mecánica (tamaño del
soporte, tipo de conector) y de la fuente de alimentación (ver más abajo).

## Notas de integración

- El YAML ya avisa (comentario "OJO CON EL TIMER LEDC") de que los servos van
  en los canales LEDC 4 y 6 para no chocar con el reloj de la cámara — eso no
  cambia con ningún sustituto.
- Los originales (D03012/D531BB) usan conector JST-ZH; los reemplazos "pico"
  como el ES9051 suelen traer conector JR estándar de 2.54mm — revisar el
  cableado del pin de señal al montar cualquiera de las dos opciones.
- Si se pasa a un servo de mayor par (DS3218/DS3225), revisar la
  alimentación: tiran de más corriente en el arranque/parada que un servo
  pico o micro, así que conviene alimentarlos aparte de la lógica del ESP32
  (mismo razonamiento que motiva el [TPS63020](tps63020-buck-boost.md) como
  regulador dedicado en el nodo solar).

---

*Documento de referencia de hardware, generado a partir de la foto
[`img/servo.png`](img/servo.png) y las fichas de
[Blue Arrow D03012](https://servodatabase.com/servo/blue-arrow/d03012) y
[Arced D531BB](https://servodatabase.com/servo/arced/d531bb) en
ServoDatabase. Ninguno de los servos mencionados está montado todavía en un
YAML activo.*
