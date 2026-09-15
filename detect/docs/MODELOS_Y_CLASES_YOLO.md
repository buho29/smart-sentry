# Modelos y clases de detección de YOLO

## Modelos disponibles

### En este repo

`model_name` es un string libre (`CameraConfig.model_name`, y el parámetro
`model_name` de los endpoints de `/detect-file` y `/config/inference`):
`get_model()` simplemente hace `YOLO(f"{model_name}.pt")`, así que basta con
poner el nombre para usarlo — se descarga solo la primera vez si no hay un
`.pt` local con ese nombre.

| Modelo | Familia | Notas |
|--------|---------|-------|
| `yolo11n` | YOLO11 nano | usado en tests, más ligero/rápido |
| `yolo11m` | YOLO11 medium | valor por defecto de `CameraConfig.model_name` |
| `yolo26m` | YOLO26 medium | ejemplo en README: ≈ 29 % de una GTX 1080 por cámara |
| `yolo26n` | YOLO26 nano | fichero presente en `detect/`, pero no referenciado en código/tests/README |
| `rtdetr-l` | RT-DETR large | ver nota más abajo |

### Modelos de Ultralytics descargables

Como `model_name` no está restringido a los `.pt` que ya hay en el repo,
también se puede usar cualquier modelo de detección que ofrezca Ultralytics
poniendo su nombre — se descarga automáticamente la primera vez. El sufijo
de tamaño es común a casi todas las familias: **n**ano < **s**mall <
**m**edium < **l**arge < **x**-large (a más grande, más preciso y más
lento).

| Familia | Tamaños | Documentación |
|---------|---------|---------------|
| YOLO11 | n, s, m, l, x | https://docs.ultralytics.com/models/yolo11/ |
| YOLO26 | n, s, m, l, x | https://docs.ultralytics.com/models/yolo26/ |
| YOLOv8 | n, s, m, l, x | https://docs.ultralytics.com/models/yolov8/ |
| RT-DETR | l, x | https://docs.ultralytics.com/models/rtdetr/ |

Referencias generales:
- Índice de todos los modelos soportados por Ultralytics:
  https://docs.ultralytics.com/models/
- Tabla comparativa de tamaño/mAP/velocidad de los modelos de detección:
  https://docs.ultralytics.com/tasks/detect/#models

## Dataset COCO

Los modelos que usa este servicio (`yolo11n`, `yolo11m`, `yolo26m`, `rtdetr-l`)
están entrenados sobre el dataset **COCO**, que tiene **80 clases**.

El índice de la izquierda es el que se pasa en el campo `classes` de la config
de la cámara (`CameraConfig.classes: list[int]`) y en el endpoint
`POST /cameras/{camera_id}/config/inference`. `null` / omitirlo = detectar todas
las clases.

Ejemplo: para detectar solo personas, perros y gatos:

```json
{ "classes": [0, 15, 16] }
```

## Tabla completa

| Índice | Nombre (YOLO / inglés) | Español |
|-------:|------------------------|---------|
| 0  | person          | persona |
| 1  | bicycle         | bicicleta |
| 2  | car             | coche |
| 3  | motorcycle      | motocicleta |
| 4  | airplane        | avión |
| 5  | bus             | autobús |
| 6  | train           | tren |
| 7  | truck           | camión |
| 8  | boat            | barco |
| 9  | traffic light   | semáforo |
| 10 | fire hydrant    | boca de incendios |
| 11 | stop sign       | señal de stop |
| 12 | parking meter   | parquímetro |
| 13 | bench           | banco (asiento) |
| 14 | bird            | pájaro |
| 15 | cat             | gato |
| 16 | dog             | perro |
| 17 | horse           | caballo |
| 18 | sheep           | oveja |
| 19 | cow             | vaca |
| 20 | elephant        | elefante |
| 21 | bear            | oso |
| 22 | zebra           | cebra |
| 23 | giraffe         | jirafa |
| 24 | backpack        | mochila |
| 25 | umbrella        | paraguas |
| 26 | handbag         | bolso |
| 27 | tie             | corbata |
| 28 | suitcase        | maleta |
| 29 | frisbee         | frisbee |
| 30 | skis            | esquís |
| 31 | snowboard       | tabla de snowboard |
| 32 | sports ball     | pelota deportiva |
| 33 | kite            | cometa |
| 34 | baseball bat    | bate de béisbol |
| 35 | baseball glove  | guante de béisbol |
| 36 | skateboard      | monopatín |
| 37 | surfboard       | tabla de surf |
| 38 | tennis racket   | raqueta de tenis |
| 39 | bottle          | botella |
| 40 | wine glass      | copa de vino |
| 41 | cup             | taza |
| 42 | fork            | tenedor |
| 43 | knife           | cuchillo |
| 44 | spoon           | cuchara |
| 45 | bowl            | cuenco |
| 46 | banana          | plátano |
| 47 | apple           | manzana |
| 48 | sandwich        | sándwich |
| 49 | orange          | naranja |
| 50 | broccoli        | brócoli |
| 51 | carrot          | zanahoria |
| 52 | hot dog         | perrito caliente |
| 53 | pizza           | pizza |
| 54 | donut           | dónut |
| 55 | cake            | pastel |
| 56 | chair           | silla |
| 57 | couch           | sofá |
| 58 | potted plant    | planta en maceta |
| 59 | bed             | cama |
| 60 | dining table    | mesa de comedor |
| 61 | toilet          | inodoro |
| 62 | tv              | televisión |
| 63 | laptop          | portátil |
| 64 | mouse           | ratón (de ordenador) |
| 65 | remote          | mando a distancia |
| 66 | keyboard        | teclado |
| 67 | cell phone      | teléfono móvil |
| 68 | microwave       | microondas |
| 69 | oven            | horno |
| 70 | toaster         | tostadora |
| 71 | sink            | fregadero |
| 72 | refrigerator    | nevera |
| 73 | book            | libro |
| 74 | clock           | reloj |
| 75 | vase            | jarrón |
| 76 | scissors        | tijeras |
| 77 | teddy bear      | oso de peluche |
| 78 | hair drier      | secador de pelo |
| 79 | toothbrush      | cepillo de dientes |

## Lista de nombres (orden por índice)

```
person, bicycle, car, motorcycle, airplane, bus, train, truck, boat,
traffic light, fire hydrant, stop sign, parking meter, bench, bird, cat, dog,
horse, sheep, cow, elephant, bear, zebra, giraffe, backpack, umbrella, handbag,
tie, suitcase, frisbee, skis, snowboard, sports ball, kite, baseball bat,
baseball glove, skateboard, surfboard, tennis racket, bottle, wine glass, cup,
fork, knife, spoon, bowl, banana, apple, sandwich, orange, broccoli, carrot,
hot dog, pizza, donut, cake, chair, couch, potted plant, bed, dining table,
toilet, tv, laptop, mouse, remote, keyboard, cell phone, microwave, oven,
toaster, sink, refrigerator, book, clock, vase, scissors, teddy bear,
hair drier, toothbrush
```

## Nota sobre `rtdetr-l`

`rtdetr-l.pt` (RT-DETR de Ultralytics) también viene entrenado con COCO, así que
usa exactamente estas mismas 80 clases y los mismos índices.

## Comprobar en tiempo de ejecución

Para ver el diccionario `índice -> nombre` real que carga un modelo concreto:

```python
from ultralytics import YOLO
print(YOLO("yolo11m.pt").names)
```

*Documento generado con IA; revisar los valores antes de montar.*
