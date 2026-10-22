# Mecanismo de disparo: Bambu Lab Electric Water Spray Kit 01 y Kit 02

Ficha de referencia de los dos kits de "pistola de agua eléctrica" de Bambu
Lab que se han considerado para el nodo final del proyecto (ver README,
sección "water-tower-defense"). No son piezas genéricas de bazar sino
**kits oficiales de Bambu Lab** pensados para montar réplicas de juguete
imprimibles en 3D: traen todo lo que no se imprime (mecanismo, batería,
cargador) y la carcasa se baja de MakerWorld. Aquí se reaprovecha solo el
mecanismo eléctrico, no la carcasa de juguete.

- **[Kit 02 (ZC005)](#kit-02-zc005)**: el que se usa. Está montado (carcasa
  T-15), medido en banco y es el que lleva el diseño de la
  [water tower](../../3D/water-tower/README.md).
- **[Kit 01 (ZC004)](#kit-01-zc004)**: el hermano mayor, a 7.4V, con más
  alcance y **bomba de autollenado**. Solo se tiene su modelo 3D; no se ha
  probado.

Los modelos 3D de los dos mecanismos están junto a esta ficha:
[`Electric Water Spray Kit 01/`](Electric%20Water%20Spray%20Kit%2001/) y
[`Electric_Water_Spray_Kit_02/`](Electric_Water_Spray_Kit_02/) (`.stp`, en
mm). En la carpeta del Kit 02 están también los dos vídeos de las pruebas de
disparo.

## Comparativa

Datos de fábrica (tienda de Bambu Lab), salvo donde se indica.

| | **Kit 01** | **Kit 02** |
|---|---|---|
| Código | **ZC004** | **ZC005** |
| Módulo del mecanismo | Gearbox **S06** | Módulo bomba **S05** |
| Tensión | **7.4V** (batería 2S) | **3.7V** (batería 1S) |
| Batería de serie | 18650 **7.4V 2200mAh** | 18650 **3.7V 2600mAh** |
| Consumo | **1A** el mecanismo + **0.5A** la bomba | **~0.9A** |
| Alcance | **10 m** | 7-8 m (medido: ~5.5 m a 0°, ~7.5 m a 30°) |
| Cadencia | **2 disparos/s** | 8 disparos/s (medido: ~4.8/s) |
| Autollenado | **Sí**, bomba que aspira de un depósito abierto | No, se rellena a mano |
| Tamaño / peso | ~53×29.5×205 mm, 278.5 g con embalaje | ~100×135×45 mm, 133.9 g |
| Tubo de silicona | No incluido (3×5 mm, RC021, se vende aparte) | Incluido |
| Carcasa oficial | T-10 Water Toy Gun (ZH054), de @QYM3D | [TEC-9 / T-15](https://makerworld.com/en/models/689632) |
| Estado en el proyecto | Solo el `.stp` | Montado y medido |

## Kit 02 (ZC005)

![Contenido del Electric Water Spray Kit 02 (ZC005): módulo bomba S05, microswitch, batería 18650, cargador USB, tubo y tornillería](img/ZC005.webp)

La carcasa que se imprimió aquí es la **T-15** del modelo MakerWorld
["TEC-9 / T-15 water toy gun"](https://makerworld.com/en/models/689632), el
modelo oficial para este kit. Hay otros modelos que usan el mismo kit, como
["FMG9"](https://makerworld.com/en/models/770810-fmg9-water-spray-electric-water-spray-kit-02).

### Identificación

- **Kit:** Bambu Lab "Electric Water Spray Kit 02", pieza interna
  identificada como **ZC005**. El Kit 01 es el **ZC004**; los códigos ZH
  (ZH054, ZH055...) son los de las carcasas de juguete que vende Bambu Lab
  para cada kit.
- **Motor:** DC, **3.7V / ~0.9A** en funcionamiento normal.
- **Batería de serie:** 18650 Li-ion **3.7V 2600mAh**, con cargador externo
  USB de **5V 1-2A** por conector **PH2.0** (el cargador solo carga la
  batería; no es la alimentación del motor, que sale directo de la 18650).
- **Prestaciones de fábrica:** alcance 7-8m, **8 ráfagas/segundo**, 133.9g,
  ~100×135×45mm. En banco se han medido ~4.8 disparos/s y ~7 m (ver
  [Medidas en banco](#medidas-en-banco)).
- **Incluye:** el módulo motor+mecanismo, la batería 18650, el cargador y el
  pulsador de disparo. **No incluye bomba de autollenado** (eso es exclusivo
  del [Kit 01](#kit-01-zc004)): el depósito se rellena a mano.

### Cómo dispara (mecanismo)

Coincide con lo observado a simple vista: un **motor DC mueve un husillo**
(tornillo sin fin) que empuja/retrae un émbolo cargado por muelle contra su
recorrido; al llegar al final del husillo, un perfil de leva **suelta el
émbolo de golpe**, que dispara el chorro de agua por la energía acumulada en
el muelle — no es el motor empujando el agua directamente, es un mecanismo
de **carga y disparo por leva**, como un percutor. Mientras el motor sigue
girando, el ciclo se repite una y otra vez, dando el disparo **a ráfagas**
(los 8 "rounds/segundo" de fábrica) en vez de un chorro continuo — de ahí
también la "función de recoil simulado" que anuncia Bambu Lab: cada ráfaga
da un tirón mecánico perceptible.

De fábrica el disparo es puramente mecánico/eléctrico simple: un **pulsador**
en serie con el motor y la batería 18650. Eléctricamente es un motor DC de
bajo consumo activado por un interruptor — no lleva ninguna electrónica de
control propia que haya que respetar ni "hablar" con protocolo alguno.

### Integración en el proyecto

- **Sustituir el pulsador por un GPIO del ESP32-S3** con un MOSFET en vez
  de un relé: es un motor DC de bajo consumo (0.9A en marcha), conmuta en
  silencio y sin desgaste. "Disparar una ráfaga" desde firmware es
  simplemente poner el GPIO en alto el tiempo necesario para que el husillo
  complete uno o varios ciclos de leva: **solo on/off, no un PWM**. Hay dos
  variantes, las dos con un **1N4007** (franja hacia el +) y un cerámico
  **104 (100nF)** en los bornes del motor:
  - **[IRLZ44N](irlz44n-mosfet.md) cortando la GND del motor** (montaje del
    autor, que ya lo tenía): 1 MOSFET, 220Ω y 10k. Cortar la GND vale aquí
    porque el motor solo tiene dos cables y ninguno va al ESP32.
  - **[IPP120P04P4L-03](ipp120p04p4l-p-mosfet.md) cortando el positivo**,
    con el mismo circuito que corta los servos (PN2222A, 10k, 1k, 10k),
    basado en el artículo de Taste The Code
    [Sensor Power Switch Using an ESP32 and P-Channel MOSFET](https://www.tastethecode.com/sensor-power-switch-using-an-esp32-and-p-channel-mosfet).
    Para quien replique el proyecto: un solo tipo de MOSFET para todo.
- **Corriente a presupuestar:** 0.9A en marcha es modesto comparado con los
  servos de la torreta (ver [`servos-pan-tilt.md`](servos-pan-tilt.md)),
  pero un motor DC puede dar un **pico de arranque/atasco de 3-5× la
  corriente nominal** (~2.7-4.5A) si el husillo se traba — dimensionar el
  MOSFET y cualquier fusible/PTC de protección con ese margen, no solo con
  los 0.9A nominales.
- **Tensión: no hace falta pasar por el riel de 5V.** El motor de fábrica ya
  corre a 3.7V, que es prácticamente la tensión nativa de la batería Li-ion
  1S del nodo (3.0-4.2V) descrita en
  [`cn3791-mppt-charger.md`](cn3791-mppt-charger.md). Lo más simple es
  alimentar este motor **directamente desde la batería principal**, en
  paralelo con la entrada del [TPS63020](tps63020-buck-boost.md) y del
  [TPS61088](tps61088-boost-converter.md), en vez de sacarlo de una salida
  ya regulada — así no se añade esta carga al presupuesto de corriente de
  los rieles de la cámara (3.3V) ni de los servos (5V).
- **Batería:** se puede prescindir de la 18650 propia del kit y alimentar el
  motor desde la batería única del nodo, o mantenerla como una segunda
  batería dedicada solo al disparo si se prefiere aislar esa carga por
  completo del resto del sistema — a decidir según cómo evolucione el
  diseño final del "espantapájaros".
- **Sin bomba de autollenado:** el depósito de agua de este kit hay que
  rellenarlo a mano; si el nodo final necesita recarga automática, esa pieza
  vendría del [Kit 01](#kit-01-zc004) (u otra solución aparte), no de este
  componente.

Esquemas de las dos variantes del interruptor:

![Esquema: disparo de la pistola por GND con IRLZ44N](img/water-gun-switch-irlz44n.svg)

*Esquema generado con IA; no verificado en banco.*

![Esquema: disparo de la pistola por el positivo con IPP120P04P4L-03 y PN2222A](img/water-gun-switch-pmosfet.svg)

*Esquema generado con IA; no verificado en banco.*

### Medidas en banco

Tomadas con la pistola montada de fábrica (carcasa T-15 impresa), antes de
desmontar el mecanismo, como referencia para comparar una vez integrado en
la torreta.

| Medida | Valor | Notas |
|---|---|---|
| Consumo (batería a medias) | **67 ml en 35 s ≈ 1.9 ml/s** | Disparo continuo, batería de serie |
| Consumo (batería cargada) | **66 ml en 31 s ≈ 2.1 ml/s (~128 ml/min)** | Disparo continuo, batería de serie |
| Cadencia | **~4.8 disparos/s** | Batería cargada. Audio del vídeo a 60 fps a velocidad normal: un golpe cada 0.207 s. El vídeo a cámara lenta 1/4× da un golpe y un chorro cada 0.84 s, que es lo mismo. La ficha de fábrica dice 8/s |
| Agua por disparo | **~0.45 ml** | 2.1 ml/s ÷ 4.8 disparos/s |
| Alcance a 0° | **~5.5 m** | Donde cae el grueso del agua, con la pistola a 1.2 m de altura y sin apenas viento. Otro día dio ~5 m |
| Alcance a 20° hacia arriba | **~6.5 m** | Misma prueba. Otro día dio ~7 m |
| Alcance a 30° hacia arriba | **~7.5 m** | Misma prueba |
| Modelo del chorro | **~18 m/s, k ≈ 0.24 /m** | Tiro parabólico con rozamiento cuadrático ajustado a las tres medidas sin viento (error ~0.4 m). Velocidad y rozamiento salen poco definidos por separado, pero el alcance sí: el mejor ángulo es ~30° y da ~7.1-7.5 m desde 1.2 m, en línea con los 7-8 m de la ficha |

#### Viento

En las pruebas se ha visto que **el viento afecta bastante a la distancia y
desvía mucho el chorro**. Tiene sentido: el chorro sale a pulsos de ~0.45 ml
que se rompen en gotas, y las gotas pequeñas se dejan arrastrar por el aire
con facilidad (es el mismo rozamiento alto que limita el alcance).

Para hacerse una idea, con el modelo del chorro ajustado arriba, desde la
boquilla de la torreta (1.86 m) y metiendo el viento en el rozamiento:

| Tilt hacia arriba | Sin viento | Viento lateral 2 m/s | Viento lateral 4 m/s | De cara 4 m/s | A favor 4 m/s |
|---|---|---|---|---|---|
| 0° | 6.0 m | se desvía 0.9 m | se desvía 1.7 m | 3.7 m | 8.2 m |
| 15° | 7.1 m | se desvía 1.4 m | se desvía 2.9 m | 3.4 m | 10.9 m |
| 30° | 7.3 m | se desvía 2.0 m | se desvía 4.0 m | 2.5 m | 12.5 m |

Son estimaciones del modelo, no medidas, pero cuadran con lo observado. Lo
que se saca de ellas:

- Una brisa de 2 m/s (apenas se nota) ya desvía el chorro un metro o más.
- Cuanto más alto se apunta, más tiempo pasa el agua en el aire (0.8 s a 0°,
  1.5 s a 30°) y más la arrastra el viento.
- Con viento de cara el alcance cae a la mitad; a favor puede llegar más
  lejos de lo normal.

Si el disparo tiene que ser preciso, conviene tener en cuenta el viento: no
disparar con viento fuerte, o corregir el apuntado con la velocidad y la
dirección del viento.

## Kit 01 (ZC004)

Kit pensado para la carcasa **T-10 Water Toy Gun** (ZH054) de @QYM3D. No se
ha comprado ni probado: todo lo de esta sección sale de la ficha de la
tienda de Bambu Lab y del modelo 3D.

### Identificación

- **Kit:** Bambu Lab "Electric Water Spray Kit 01", código **ZC004**.
- **Incluye:** gearbox **S06** (motor + mecanismo), batería 18650
  **7.4V 2200mAh**, cargador USB, **bomba de agua**, adaptador **en Y PH2.0
  de 150 mm** y tornillos M3×16. **No incluye el tubo de silicona**
  (3×5 mm transparente, 2 m, RC021, se vende aparte).
- **Consumo:** **1A** el mecanismo y **0.5A** la bomba.
- **Prestaciones de fábrica:** alcance **10 m**, **2 disparos/s**. Como el
  Kit 02, dispara a pulsos mientras se mantiene el gatillo y anuncia el mismo
  "recoil simulado", así que el mecanismo debería ser del mismo tipo (carga
  y disparo), con menos disparos por segundo y más agua en cada uno.
- **Autollenado:** la bomba aspira agua de un depósito abierto (un cubo, un
  bidón) y la mete en el depósito de la pistola, sin tener que quitarlo para
  rellenar.
- **Avisos de fábrica:** no hacerlo funcionar sin agua (puede dañar el
  mecanismo) y no sumergirlo: el interruptor y la batería no son estancos.
- **Tamaño:** ~53×29.5×205 mm y 278.5 g con embalaje: es mucho más largo
  que el Kit 02.

### Qué cambiaría si se usara en vez del Kit 02

- **Tensión de 7.4V.** Ya no vale colgarlo directamente de la batería 1S del
  nodo (3.0-4.2V) como el Kit 02. Lo más sencillo es **subir a 7V el riel de
  los servos** ([TPS61088](tps61088-boost-converter.md), ahora a 5V) y
  colgar la pistola de él, sin añadir otro convertidor:
  - **La pistola a 7V va bien.** Una 2S va de 6.0 a 8.4V (7.4V nominal), así
    que 7V es como la batería de fábrica a media carga.
  - **Los servos quedan fuera de su ficha.** El
    [DM996 de diymore](servos-pan-tilt.md#diymore-dm996-dos-versiones-con-la-misma-serigrafía)
    pone 4.8-6V. Es un clon del MG996R, que admite 4.8-7.2V, así que lo más
    probable es que aguante, pero a 7V tira más corriente (bloqueo ~2.9A
    frente a 2.5A a 6V) y se calienta más; y en la versión barata hubo servos
    que echaron humo por debajo de 6V. Antes de fiarse, probar un DM996 a 7V
    un rato y tocar la temperatura. Si no aguanta, el recambio es el
    [Miuzei de 15 kg 180°](servos-pan-tilt.md#alternativas-digitales-familia-ds3218),
    que admite hasta 7.4V.
  - **El boost da menos corriente.** El límite de 10A del TPS61088 es de
    entrada: con la celda a 3.3V y ~90% de rendimiento salen ~5.9A a 5V,
    pero solo **~4.2A a 7V**. Disparar mientras se mueven dos servos (~2.5A)
    cabe; lo que puede hundir el riel y hacer temblar los servos es el pico
    de arranque del motor (3-5A). Mejor no arrancar el motor a la vez que se
    mueven los servos, y dejar el condensador de 1000-2200µF en el riel. En
    ese caso la batería 1S tiene que dar ~6A (2.5A × 7V / (0.9 × 3.3V)).

  Para 7V no hay puente en el módulo: se pone una resistencia en ADJ (o se
  usa la versión con potenciómetro) y se mide VOUT antes de conectar nada.
  Mientras se use el Kit 02, el riel sigue en 5V.

  Las alternativas son mantener la batería 2S propia del kit o poner un
  segundo TPS61088 a 7.4V solo para la pistola. Los MT3608 y XL6009 no
  llegan: desde la 1S, 1.5A a 7.4V son unos 3.3A de entrada más el pico de
  arranque (ver [`step-up-boost-comparativa.md`](step-up-boost-comparativa.md)).
- **Dos cargas en vez de una.** El adaptador en Y reparte la batería entre
  el mecanismo y la bomba. Para mandar las dos desde el ESP32-S3 harían
  falta **dos salidas con MOSFET**, una para disparar y otra para rellenar,
  las dos colgadas del riel de 7V. Sirve cualquiera de los dos circuitos de
  arriba, cada uno con su diodo y su condensador; con el IPP120P04P4L-03 la
  puerta queda a −7V, dentro de sus ±16V. Con el IRLZ44N y una batería
  aparte para la pistola, la GND de esa batería tiene que ir unida a la del
  ESP32.
- **No dejar la bomba en seco.** Si el firmware controla el rellenado,
  conviene limitarlo por tiempo (o con un sensor de nivel) para no hacerla
  girar sin agua cuando el depósito exterior se vacíe.
- **Menos cadencia, más alcance.** 2 disparos/s frente a ~4.8 medidos en el
  Kit 02, a cambio de 10 m de fábrica frente a 7-8 m. No se sabe cuánto agua
  echa por disparo ni cómo le afecta el viento hasta medirlo.
- **No cabe en la water tower actual.** El diseño de la
  [water tower](../../3D/water-tower/README.md) está hecho alrededor del
  Kit 02 (tambor de 100 mm); con 205 mm de largo, el Kit 01 pediría
  rediseñar el tambor y recalcular el par de los servos con el peso extra.

---

*Documento de referencia de hardware. Datos del Kit 02 recopilados de la
ficha oficial de Bambu Lab, Alibaba y el modelo MakerWorld TEC-9 / T-15 que
usa este mismo kit, más las medidas en banco; datos del Kit 01 solo de la
ficha de la tienda de Bambu Lab. Componentes aún no integrados en ningún
YAML del proyecto.*

*Documento generado con IA; revisar los valores antes de montar.*
