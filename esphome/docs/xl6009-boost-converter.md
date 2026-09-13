# Módulo elevador de tensión XL6009 (4A)

Ficha de referencia del módulo boost XL6009, comparado con los otros dos
elevadores ya documentados ([MT3608](mt3608-boost-converter.md) y
[TPS63020](tps63020-buck-boost.md)) para decidir cuál conviene en un nodo a
batería con deep sleep (ver README, "water-tower-defense").

## Identificación

- **Chip principal:** **XL6009** (XLSemi) — convertidor **boost/buck-boost/
  inversor** configurable con un único pin de feedback, frecuencia de
  conmutación fija **400kHz**, hasta **4A de corriente de conmutación**
  ([datasheet XLSemi](https://www.xlsemi.com)).
- **Entrada:** 5-32V. **Salida:** ajustable por potenciómetro (trimpot) en la
  placa, igual que el MT3608 — de fábrica puede estar en cualquier posición,
  hay que **ajustarla a 5.0V con un multímetro antes de conectar carga**.
- **Diferencia frente a MT3608 y TPS63020:** el chip XL6009 sí tiene un pin
  **EN** dedicado (activo alto, apagado si se lleva a bajo) según su
  datasheet — pero en la práctica esto es irrelevante para el módulo típico
  de AliExpress/Amazon con trimpot: esas placas **no sacan el pin EN a
  ningún pad**, lo dejan flotando (por defecto a nivel alto = siempre
  encendido). Es el mismo problema que el MT3608: sin modificar la placa,
  no hay forma de apagar el regulador salvo cortando la entrada VIN.

## Consumo en reposo (quiescent current): los tres módulos comparados

Esto es lo que importa para un nodo con deep sleep, donde el regulador va a
pasar la mayor parte del tiempo sin entregar corriente útil pero conectado a
la batería:

| Módulo | Consumo con el regulador habilitado, sin carga | Consumo si se puede deshabilitar | ¿El breakout típico expone el pin de apagado? |
| --- | --- | --- | --- |
| **XL6009** | **Iq = 2.5-5mA típ/máx** (VEN=2V, VFB=Vin) — el más alto de los tres | 70-100µA (VEN=0V) — sigue sin ser un apagado real | **No** (EN existe en el chip pero no está cableado a ningún pad en las placas con trimpot habituales) |
| **MT3608** | 100-200µA en modo PFM (carga ligera) a 1.6-2.2mA en modo PWM (el módulo alterna solo) | No aplica — el chip **no tiene pin EN** | **No** (el chip mismo carece de control de apagado) |
| **TPS63020** | **~25µA típ** en modo Power Save, sin carga | **<1µA** con EN a nivel bajo — apagado real del regulador | **Sí**, normalmente presente como pad `EN` en el módulo |

Fuentes: XL6009 — tabla "Electrical Characteristics (DC Parameters)" del
datasheet XLSemi (Iq 2.5mA típ / 5mA máx con EN=2V; ISTBY 70-100µA con
EN=0V). MT3608 — datasheet Aerosemi (100-200µA en PFM, 1.6-2.2mA en PWM).
TPS63020 — datasheet TI (IQ ≈25µA en power save, EN=bajo = apagado).

## Lectura práctica para este proyecto

- **El XL6009 es el peor de los tres para deep sleep**, no el mejor: aunque
  su chip tiene EN, el módulo real que "el usuario ya tiene disponible" (con
  trimpot, sin ese pin accesible) va a consumir **2.5-5mA de forma
  continua** mientras la batería esté conectada — muy por encima del propio
  consumo en deep sleep de una ESP32 (decenas de µA). En una batería 1S
  típica de 2000mAh, eso solo ya se comería el equivalente a semanas de
  autonomía en reposo.
- **El MT3608 es intermedio**: en el mejor caso (PFM, sin carga) baja a
  100-200µA, mucho mejor que el XL6009, pero sigue siendo "siempre
  encendido" porque el chip no tiene forma de apagarse.
- **Reparto final adoptado:** TPS63020 a 3.3V (lógica, por el pin `3V3`) +
  [TPS61088](tps61088-boost-converter.md) a 5V (servos), ver
  [`step-up-boost-comparativa.md`](step-up-boost-comparativa.md). El
  XL6009+IRLZ44N queda como alternativa descartada.
- **El TPS63020 sigue siendo la mejor opción para el riel que alimenta la
  lógica en un nodo con deep sleep** (como ya apuntaba
  [`tps63020-buck-boost.md`](tps63020-buck-boost.md#notas-de-integración-con-el-proyecto)
  sobre el modo Power Save): no solo su consumo habilitado sin carga (~25µA)
  ya es 100-200x menor que el del XL6009, sino que además su pin `EN` sí
  suele estar accesible en el módulo y permite un apagado real (<1µA) si el
  diseño del nodo lo controla, por ejemplo desde un GPIO de la ESP32 antes de
  entrar en deep sleep.
- **Para los servos** (donde hoy se recomendaba el MT3608 como riel
  separado, ver [`mt3608-boost-converter.md`](mt3608-boost-converter.md#rol-en-el-proyecto)),
  el XL6009 no aporta nada mejor: misma falta de apagado, consumo en reposo
  peor, y su ventaja real (más corriente de pico anunciada) ya se vio en la
  otra ficha que no se sostiene en la práctica más allá de ~1-1.5A
  sostenidos con este tipo de módulo genérico.
- **Si de todos modos se quiere usar el XL6009** (por ejemplo, porque ya se
  tiene y para servos que no duermen), la única forma de que no drene la
  batería en reposo es **cortar su VIN con un interruptor externo**
  controlado por la ESP32, exactamente igual que haría falta con el MT3608 —
  el pin EN interno del chip no sirve de nada si el módulo no lo saca a un
  pad. Ver el diseño concreto con IRLZ44N más abajo.

## Rol previsto: XL6009 como riel de servos, cortado por un IRLZ44N

Descartado como riel principal, el plan para este módulo es usarlo **solo
para los servos de la torreta**, con su consumo en reposo (2.5-5mA)
eliminado mediante un interruptor de carga: un MOSFET **IRLZ44N** en
conmutación de **lado bajo** (low-side), controlado por un GPIO de la
ESP32.

- **Conexión:** batería → `VIN` del XL6009 (directo, sin pasar por el
  interruptor) → salida `VOUT` a los servos. El retorno a masa del módulo
  (`GND`) va al `drain` del IRLZ44N; el `source` va a la masa común del
  sistema; el `gate` al GPIO de la ESP32 (con una resistencia serie de
  ~100-220Ω al gate y una **resistencia pull-down de ~10kΩ entre gate y
  source**, para forzar el MOSFET a apagado mientras el GPIO esté flotante
  durante el arranque/reset de la ESP32, momento en que los GPIO no tienen
  un nivel definido).
- **Secuencia:** la ESP32 pone el GPIO a nivel alto solo cuando va a mover
  los servos, y lo vuelve a bajo antes de entrar en deep sleep. Con el
  MOSFET cortado, el consumo del riel de servos (XL6009 + servos) cae a
  prácticamente 0 durante el sueño, en vez de los 2.5-5mA continuos del
  módulo solo.
- **Aviso sobre el gate a 3.3V:** el IRLZ44N es "logic level" pero sus
  cifras de RDS(on) del datasheet (~22-29mΩ) están medidas a Vgs=4-5V; a los
  3.3V que da un GPIO de la ESP32 el canal solo se satura parcialmente y el
  RDS(on) real sube a **~60mΩ o más**. Con los picos de stall de dos
  servos DS3218MG/DS3225MG (~4.2A combinados, ver
  [`servos-pan-tilt.md`](servos-pan-tilt.md)), eso son del orden de **1W
  disipados en el MOSFET** en el peor caso — asumible en un TO-220 sin
  heatsink para picos cortos e intermitentes (el propio stall), pero conviene
  vigilarlo si en algún momento los servos se mantienen forzados contra un
  tope de forma sostenida. Si hiciera falta más margen térmico, la opción
  simple es sustituir el IRLZ44N por un MOSFET realmente especificado a
  Vgs=3.3V (p. ej. AO3400, IRLML6344) sin cambiar el resto del diseño.

---

*Documento de referencia de hardware. Componente aún no cableado en ningún
YAML del proyecto.*
