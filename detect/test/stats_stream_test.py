import time
import re
import requests

url = "http://192.168.1.56:8080/"
resp = requests.get(url, stream=True)

# Sacar el boundary del Content-Type de la respuesta
content_type = resp.headers.get("Content-Type", "")
m = re.search(r"boundary=(\S+)", content_type)
if not m:
    raise RuntimeError(f"No se encontró boundary en Content-Type: {content_type}")
boundary_marker = b"--" + m.group(1).strip('"').encode()

buf = b""
last_frame_time = time.monotonic()
interval_start = time.monotonic()
frame_dts_ms = []

state = "SEEK_BOUNDARY"
content_length = None

for chunk in resp.iter_content(chunk_size=1024):
    buf += chunk

    while True:
        if state == "SEEK_BOUNDARY":
            idx = buf.find(boundary_marker)
            if idx == -1:
                break
            buf = buf[idx + len(boundary_marker):]
            state = "READ_HEADERS"

        elif state == "READ_HEADERS":
            header_end = buf.find(b"\r\n\r\n")
            if header_end == -1:
                break
            headers_raw = buf[:header_end].decode(errors="ignore")
            buf = buf[header_end + 4:]

            cl_match = re.search(r"Content-Length:\s*(\d+)", headers_raw, re.IGNORECASE)
            if not cl_match:
                state = "SEEK_BOUNDARY"  # cabecera rara, resincroniza
                continue
            content_length = int(cl_match.group(1))
            state = "READ_BODY"

        elif state == "READ_BODY":
            if len(buf) < content_length:
                break  # el frame aún no ha llegado completo

            # frame = buf[:content_length]  # aquí tendrías el JPEG completo si lo necesitas
            buf = buf[content_length:]

            now = time.monotonic()
            dt = now - last_frame_time
            last_frame_time = now
            frame_dts_ms.append(dt * 1000)

            if buf[:2] == b"\r\n":
                buf = buf[2:]

            state = "SEEK_BOUNDARY"

            if now - interval_start >= 1.0:
                if frame_dts_ms:
                    count = len(frame_dts_ms)
                    duration = now - interval_start
                    fps = count / duration
                    avg_ms = sum(frame_dts_ms) / count
                    min_ms = min(frame_dts_ms)
                    max_ms = max(frame_dts_ms)
                    print(
                        f"FPS: {fps:.1f} | Min: {min_ms:.0f}ms | Max: {max_ms:.0f}ms |"
                        f" Avg: {avg_ms:.0f}ms (Frames: {count})"
                    )
                frame_dts_ms = []
                interval_start = now