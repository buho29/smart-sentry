
Crea un entorno virtual desde la terminal integradaAbre la terminal de VS Code (Ctrl+ñ o Terminal > New Terminal) y ejecuta: python -m venv venv Esto crea una carpeta venv dentro de tu proyecto.
5
Activa el entorno virtualEn PowerShell: venv\Scripts\Activate.ps1 Si da error de política de ejecución de scripts, ejecuta antes: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass VS Code normalmente detecta el venv y te pregunta si quieres usarlo como intérprete por defecto — di que sí.

2. Comprueba que el venv sigue activado en esa terminal
Debe verse (venv) al principio de la línea. Si no está, actívalo de nuevo:

venv\Scripts\Activate.ps1

3. Vuelve a lanzar el dashboard y mira qué dice la terminal

cd C:\cupula-de-agua\esphome
python -m venv venv
venv\Scripts\activate
pip install esphome

esphome dashboard .
esphome run esp32-s3-cam.yaml
esphome compile huerta.yaml
esphome logs huerta.yaml
esphome logs huerta.yaml --device COM10
