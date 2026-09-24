# GPU: los dos problemas de la GTX 1080

Todo lo que se sabe sobre por qué la detección falla o va lenta por culpa de la
tarjeta, reunido en un sitio. Antes estaba repartido entre el README,
[`CICLO-DE-VIDA.md`](CICLO-DE-VIDA.md) y [`ARQUITECTURA.md`](ARQUITECTURA.md).

## Resumen

Son **dos problemas distintos** que se confunden porque dan síntomas parecidos
(detecciones raras, inferencia lenta):

| | cuDNN / `misaligned address` | Bajada de reloj (P-state) |
| --- | --- | --- |
| Qué pasa | Error CUDA intermitente o detecciones erráticas | El driver baja el reloj con carga ligera: la inferencia va 2-3 veces más lenta y, **en esta tarjeta**, sale corrupta |
| A quién afecta | Pascal (GTX 10xx) | Cualquier GeForce en Windows; la corrupción, probablemente solo Pascal |
| Qué se hace hoy | `CUDNN_ENABLED = False` en `main.py` | Modelo pesado (`yolo26m`) + keep-alive |
| Con una RTX 20 o posterior | Desaparece: cuDNN activado | Se fija el reloj con `nvidia-smi -lgc` y el keep-alive sobra |

Configuración actual en esta máquina: GTX 1080, driver 581.80, torch
2.6.0+cu124, cuDNN 9.1.0.

## 1. cuDNN y `CUDA misaligned address`

**Síntoma.** Con cuDNN activo, la GTX 1080 da `CUDA misaligned address` de forma
intermitente. A veces días después de arrancar, que es de los fallos más
difíciles de atribuir. Otras veces no da ningún error y las detecciones
simplemente salen erráticas.

**Qué se hace.** `CUDNN_ENABLED = False` al principio de
[`main.py`](../main.py), con **la misma línea** en `test/barrido_modelos.py`, que
no pasa por `main.py` y es el único script de `test/` con inferencia sostenida.
El servicio dice en el log en qué modo arrancó y dónde cambiarlo.

**Lo que cuesta.** Medido aquí: 9,71 ms por inferencia con cuDNN contra 11,49
sin él. En cualquier tarjeta que no sea Pascal hay que dejarlo a `True`.

**Por qué no se va a arreglar.** NVIDIA y PyTorch ya han abandonado Pascal:

- Las builds cu128 de PyTorch 2.8 en adelante no incluyen sm_61 (la 1080).
- cuDNN 9.12 deja de soportar la capacidad 6.1.
- TensorRT 10 exige una Turing (RTX 20) o posterior.
- NVIDIA considera la arquitectura "congelada" desde CUDA 12.8: un bug de cuDNN
  que solo salga en Pascal no lo va a corregir nadie.

**Techo de versiones mientras se use la 1080**: torch **cu126**, cuDNN
**9.11.1** y TensorRT **8.6**. Si alguna actualización se pasa de ahí, la
tarjeta deja de funcionar, no solo de ir bien.

## 2. La bajada de reloj (P-state)

### El mecanismo

El driver decide los relojes según la utilización. Con un modelo ligero, la GPU
pasa la mayor parte de cada ciclo sin trabajo: con la cámara a 16,7 fps (un
frame cada 60 ms) y `yolo26n` tardando ~10 ms, está parada dos tercios del
tiempo. El driver lo ve como reposo y baja a P5, **759 MHz de los 1961**.
`nvidia-smi` lo dice con todas las letras: `Clocks Event Reasons -> Idle:
Active`, con 45 W de los 210 disponibles.

### En esta tarjeta no solo va lenta: se equivoca

A esos relojes la inferencia sale **incorrecta**:

- confianzas de `1.812`, que son imposibles porque salen de una sigmoide;
- frames enteros sin detectar a una persona que estaba delante de la cámara.

`test/gpu_yolo_stress_test.py` reproduce el fallo.

| Reloj | Resultado |
| --- | --- |
| 759 MHz | corrupción masiva, deja de detectar |
| 1177 MHz | 58 corruptas en 7486 frames |
| 1290 MHz | 1 corrupta en 5986 frames |

De ahí venía una dependencia del modelo que despistó mucho: `yolo26m` ocupa 30
ms de cada 60 y mantiene la GPU despierta él solo, así que nunca fallaba;
`yolo26n` solo ocupa 10 y la dejaba dormirse. Parecía que el modelo pequeño
"detectaba peor", y lo que pasaba es que al ser más rápido dejaba que la tarjeta
se durmiera.

### Gastar menos sale más caro

Medido en la misma máquina y la misma escena:

| Configuración | ms/inferencia | Inferencia | Dummies | Total | Reloj | Corrupciones |
| --- | --- | --- | --- | --- | --- | --- |
| `yolo26n` sin keep-alive | 33,1 | 55,2 % | 0 % | **55 %** | 40 % | — |
| `yolo26n` con keep-alive | 15,0 | 25,1 % | 27,0 % | **52 %** | 61 % | 6/min |
| **`yolo26m`** | 22,6 | 36,8 % | ~0 % | **~37 %** | **97 %** | **0 en 4935** |

El driver castiga la carga baja bajando los relojes, y entonces el mismo trabajo
cuesta el doble. Apagar el keep-alive con el modelo ligero es la opción **más**
cara de las tres, y ni siquiera con keep-alive deja de corromper.

### Qué se hace hoy

1. **Un modelo lo bastante pesado** para que la GPU no se duerma sola:
   `yolo26m`. Es lo que de verdad lo resuelve.
2. **El keep-alive**, **apagado por defecto**: una inferencia dummy cuando la
   cola de frames se queda vacía. Solo hace falta si se vuelve a un modelo
   ligero. Se enciende con `POST /config/keepalive` (global y persistido);
   el intervalo es por cámara (`keepalive_interval_sec`, 0,005 s por defecto),
   se pausa solo tras 3 s sin frames (`KEEPALIVE_IDLE_LIMIT_SEC`) y la guarda
   `dummy_fits` no lanza la dummy si no cabe antes del frame siguiente, así que
   con un modelo pesado apenas actúa.

**El intervalo, 0,005 a propósito.** Con huecos de 10 ms entre dummies, el
driver sigue viendo la tarjeta ociosa y baja los relojes aunque el keep-alive
esté disparando sin parar:

| `interval_sec` | Reloj sostenido | Detecciones corruptas |
| --- | --- | --- |
| 0,01 | 847 MHz (P5) | **570 en 48.967 frames** (1,16 %), con 54.753 dummies |
| **0,005** | ~1290 MHz | **0 en 4147 frames seguidos** |

**Fijar los relojes no es posible en esta tarjeta.** `nvidia-smi -lgc`
responde "not supported for GPU", porque solo funciona desde Volta y Turing. En
Windows tampoco hay modo persistencia. La única palanca que queda es el panel de
NVIDIA: *Administrar configuración 3D → Configuración del programa →* el
`python.exe` del venv *→ Modo de administración de energía → Preferir
rendimiento máximo*. Está pensada para aplicaciones 3D y no está comprobado que
afecte a CUDA.

## 3. Diagnóstico

En `/status` de cada cámara, `corrupt_detections` cuenta las
confianzas fuera de `[0,1]`. Esas detecciones se descartan antes de propagarse,
porque una caja con confianza 1,8 movería los servos hacia un fantasma y
dispararía una grabación.

El servicio ya no lee los relojes. Para verlos, desde consola, segundo a
segundo:

```powershell
nvidia-smi --query-gpu=pstate,clocks.gr,utilization.gpu,power.draw --format=csv -l 1
```

Si el reloj está bajo, la respuesta no es tocar el keep-alive: es usar un
modelo más pesado. Suena al revés y está medido.

## 4. ¿Solo pasa en esta tarjeta?

Buscado en septiembre de 2026.

**La bajada de reloj no es exclusiva de la 1080.** Es la gestión de energía de
las GeForce en Windows ante cargas cortas a ráfagas, y está documentada en
tarjetas recientes:

- **RTX 4090 + YOLOv8 + Windows 11**
  ([foro de NVIDIA](https://forums.developer.nvidia.com/t/performance-issue-on-rtx-4090-with-ultralytics-yolov8-and-pytorch/299687)):
  oscilaba entre P2 y P8 y bajaba de 7 a 4 fps consumiendo ~20 W. Un portátil
  más modesto iba un 20 % más rápido. Respuesta de NVIDIA: con uso ligero la
  tarjeta nunca llega a subir el reloj, porque el cambio de estado tiene
  histéresis. Añaden que en Windows las GeForce priorizan ahorro y silencio
  sobre rendimiento.
- **"4090 is slower than 1080"**
  ([TensorRT#2907](https://github.com/NVIDIA/TensorRT/issues/2907)): alguien
  cambió una 1080 por una 4090 y la inferencia le iba más lenta.
- La GPU también baja el reloj cuando se
  [bloquea la sesión de Windows](https://forums.developer.nvidia.com/t/gpu-clock-throttle-idle-is-active-when-desktop-locked-windows-pytorch/221254)
  con PyTorch corriendo.

Una tarjeta más potente lo sufriría igual o más: una 4090 se "aburre" todavía
más con un modelo nano.

**El problema de cuDNN, y probablemente la corrupción, sí parecen propios de
Pascal.** No se han encontrado casos de `misaligned address` con convoluciones
cuDNN en YOLO sobre RTX 30/40/50. Los que aparecen en RTX vienen de otras
librerías: xformers
([RTX 4070](https://github.com/AUTOMATIC1111/stable-diffusion-webui/discussions/9945))
o Triton con `torch.compile`
([pytorch#96628](https://github.com/pytorch/pytorch/issues/96628)). Sí hay
casos con GTX 1080 Ti
([pytorch#32893](https://github.com/pytorch/pytorch/issues/32893)) y
detecciones que salen bien en CPU y mal en GPU
([yolov5#1391](https://github.com/ultralytics/yolov5/issues/1391),
[darknet#405](https://github.com/pjreddie/darknet/issues/405): con cuDNN,
tiny-yolo dejaba de detectar y el grande no). **Que sea solo de Pascal es una
deducción**: ningún informe lo dice expresamente, pero encaja con lo medido.

## 5. Si se cambia de tarjeta

Cualquier RTX 20 o posterior resuelve los dos problemas, cada uno por su lado.
Para esta carga, cuanto más modesta mejor: una RTX 3060 (12 GB) o una RTX 4060
van sobradas y gastan menos que la 1080.

**cuDNN**: se pone `CUDNN_ENABLED = True` (y su copia en
`test/barrido_modelos.py`).

**Reloj**: desde Turing se puede fijar, como administrador:

```powershell
nvidia-smi -lgc 1800,1800
```

- Se pierde al reiniciar: hay que relanzarlo con una tarea programada al
  arrancar Windows.
- Con el reloj fijo, el keep-alive sobra (`keepalive_enabled = false`) y
  `yolo26n` rinde a su velocidad real.
- Nadie lo ha documentado con inferencia de YOLO en una GeForce con Windows.
  Lo usa mucha gente para minería y overclock. Medirlo con
  `test/barrido_modelos.py` antes de quitar el keep-alive.

**Consumo con el reloj fijo.** No se iguala al de ir al 100 %. El consumo tiene
una parte fija (fugas, que dependen sobre todo del voltaje) y otra que depende
de la actividad. Fijar un reloj alto mantiene el voltaje alto y sube la parte
fija, pero la de actividad sigue siendo proporcional a lo que se calcula.

Orden de magnitud estimado para una RTX 4060, sin medir:

| Situación | Consumo aprox. |
| --- | --- |
| Reposo normal (P8) | ~10–15 W |
| Reloj fijo, sin hacer nada | ~30–50 W |
| Al 100 % | ~115 W (su límite) |

La diferencia es de unas decenas de vatios en 24 h: por ejemplo, 30 W de más
son ~260 kWh/año, **40–60 €/año** según tarifa. El keep-alive actual tampoco es
gratis: mantiene el reloj alto y además suma cálculo de verdad. Fijar el reloj
debería salir más barato que el keep-alive o que un modelo pesado puesto solo
para que la GPU no se duerma.

**TensorRT.** Es la recomendación principal de la comunidad de Ultralytics:
exportar con `model.export(format="engine", half=True)` y cargar el `.engine`.
Anuncian hasta 5 veces más velocidad
([Ultralytics](https://docs.ultralytics.com/integrations/tensorrt)). El
`.engine` solo vale para la tarjeta y la versión de TensorRT con que se
generó. Con TensorRT FP16, un `yolo26m` debería costar menos que el `yolo26n`
actual en PyTorch. Es una estimación, sin medir.

**Nada de esto sirve en la 1080:**

- FP16 va a **1/64** de la velocidad de FP32 en su chip, GP104
  ([guía de NVIDIA para Pascal](https://docs.nvidia.com/cuda/pascal-tuning-guide/index.html)):
  `half=True` la haría muchísimo más lenta.
- TensorRT 10 no la soporta
  ([tabla de compatibilidad](https://docs.nvidia.com/deeplearning/tensorrt/latest/getting-started/support-matrix.html)).
  TensorRT 8.6 en FP32 daría poca ganancia y choca con las versiones que espera
  ultralytics.
- INT8 sí es rápido en GP104, pero hay que calibrarlo con imágenes de la escena
  y no está documentado con YOLO en Pascal.

<details>
<summary>El camino que se recorrió hasta llegar aquí (histórico)</summary>

Esta fase quedó **desmontada a propósito**: se llegó a gobernar el keep-alive
por el reloj real de la GPU (NVML), con detección de arquitectura Pascal, un
`clock_ratio` ajustable y overrides por cámara. Mucha maquinaria para afinar un
workaround que no llegaba a arreglar el problema. Después se quitaron también
las lecturas de NVML que quedaban para mirar (`GET /gpu`, `gpu_clock_pct`), el
`selftest` y la sonda del tracker.

### La solución intermedia: gobernar por el reloj, no por un temporizador

Bajar el intervalo arregla el síntoma (medido en caliente: 759 → 1265 MHz,
22,2 → 9,8-16,6 ms, confianzas de vuelta a 0,71-0,91) pero introduce otro
problema, porque **el dummy cuesta lo mismo que una inferencia real**:

| Modelo | Inferencia | Dummy | Hueco por frame |
| --- | --- | --- | --- |
| `yolo26n` | 10,7 ms | 9,3 ms | 49,2 ms → caben ~3 |
| `yolo26m` | 29,9 ms | **37,2 ms** | **30,0 ms** → no cabe ninguno |

Con el medium, lanzar el dummy igualmente metía el ciclo en ~72 ms contra los
60 de la cámara: **se perdía en torno al 17 % de los frames** y la GPU subía al
93 %, todo ello sin necesitarlo.

Así que la condición ya no es un temporizador sino el **reloj real de la GPU**:
`gpu_underclocked()` lee `sm_clock` por NVML (cacheado 0,5 s) y calienta
mientras esté por debajo de `keepalive_clock_ratio` (**0,95**) del **máximo
observado**. Más una guarda: si el dummy no cabe antes del frame siguiente, no
se lanza. Esa guarda (`dummy_fits`) es la que protege al modelo pesado, y es lo
único de esta fase que sigue en el código.

**La referencia es el reloj observado, no el que declara NVML.**
`nvmlDeviceGetMaxClockInfo` devuelve 1961 MHz en la GTX 1080, pero ese es el de
P0: bajo carga de cómputo la tarjeta se queda en **P2**, con un techo real en
torno a 1290 MHz.

**Y el ratio va alto a propósito.** La regla es "calienta salvo que la tarjeta
esté prácticamente en su tope sostenido", no "calienta solo si ha caído un
30 %". Estuvo en 0,7 y fue un error caro: con el máximo observado en 1657 MHz el
umbral quedaba en 1160, y esta tarjeta se sostiene en 1177 —justo por encima—,
así que no se calentaba nunca. Medido: **58 detecciones corruptas en 7486
frames**, frente a **1 en 5986** cuando sí calentaba.

En una GPU sana que sostiene sus relojes, el actual es prácticamente igual al
observado, así que **no se calienta nada**: ahí estaba la portabilidad. Y sin
lecturas de reloj (`pynvml` ausente, o una AMD con ROCm, donde PyTorch también
llama `"cuda"` al device) se devolvía `False` y no se calentaba a ciegas.

</details>

*Documento generado con IA; revisar los valores antes de montar.*
