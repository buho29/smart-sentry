# -----------------------------------------------------------------------------
# Lee el stream del ESP32 con un socket crudo, sin requests/urllib3 de por
# medio -- lo más parecido posible a como lo hace un navegador por debajo.
#
# Objetivo: si esto TAMBIÉN aguanta bien el movimiento de la cámara (como el
# navegador), confirma que el problema está específicamente en cómo
# requests/urllib3 lee el stream en Windows, no en el firmware. Si esto SÍ
# se cuelga, reabre la sospecha sobre el firmware otra vez.
#
# No sirve nada por HTTP, solo imprime por consola cuánto tarda cada trozo
# de datos en llegar -- es un test de lectura pura, sin proxy.
#
# Uso:
#   python raw_socket_test.py
#   (déjalo corriendo y mueve la cámara)
# -----------------------------------------------------------------------------

import socket
import time

ESP_HOST = "192.168.1.50"
ESP_PORT = 8080

sock = socket.create_connection((ESP_HOST, ESP_PORT), timeout=10)
sock.settimeout(30)  # timeout de lectura generoso, solo para no colgar el script para siempre

request = f"GET / HTTP/1.1\r\nHost: {ESP_HOST}\r\nConnection: keep-alive\r\n\r\n"
sock.sendall(request.encode())

print("Conectado, leyendo stream por socket crudo...")

last_data_at = time.time()
total_bytes = 0
n_reads = 0

try:
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            gap = time.time() - last_data_at
            print(f"[raw] TIMEOUT: {gap:.1f}s sin ningún byte (socket.recv() no ha vuelto)")
            continue

        if not chunk:
            print("[raw] el ESP cerró la conexión (recv devolvió vacío)")
            break

        now = time.time()
        gap = now - last_data_at
        if gap > 1.0:
            print(f"[raw] aviso: {gap:.1f}s sin datos antes de este chunk ({len(chunk)} bytes)")
        last_data_at = now
        total_bytes += len(chunk)
        n_reads += 1
        if n_reads % 100 == 0:
            print(f"[raw] {n_reads} lecturas, {total_bytes/1024:.0f} KB totales")

except KeyboardInterrupt:
    print("\nParado por el usuario.")
finally:
    sock.close()
