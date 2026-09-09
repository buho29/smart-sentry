import asyncio
import time
from aioesphomeapi import APIClient

async def main():
    client = APIClient(
        address="192.168.1.50",
        port=6053,
        password="",
        noise_psk="JzwVvMMnr1H0kAtYHDnWKfZ/7L0RYVqOeGjWxwpIzRc=",
    )

    t0 = time.perf_counter()
    await client.connect(login=True)
    t_connect = time.perf_counter() - t0
    print(f"connect(): {t_connect*1000:.1f} ms")

    t0 = time.perf_counter()
    entities, services = await client.list_entities_services()
    t_list = time.perf_counter() - t0
    print(f"list_entities_services(): {t_list*1000:.1f} ms  ({len(entities)} entidades, {len(services)} servicios)")

    print(f"\n{len(entities)} entidades encontradas:\n")
    for e in entities:
        print(f"[{type(e).__name__:20s}] key={e.key:<10} object_id={e.object_id!r:30} name={e.name!r}")

    print(f"\n{len(services)} servicios encontrados:")
    for s in services:
        print(f" - {s.name} {s.args}")

    t0 = time.perf_counter()
    device_info = await client.device_info()
    t_info = time.perf_counter() - t0
    print(f"device_info(): {t_info*1000:.1f} ms")

    print("\nDevice info:")
    print(" name:", device_info.name)
    print(" esphome_version:", device_info.esphome_version)
    print(" mac_address:", device_info.mac_address)
    print(" model:", device_info.model)
    print(" has_deep_sleep:", device_info.has_deep_sleep)

    print(f"\nTOTAL hasta tener todo listo: {(t_connect + t_list + t_info)*1000:.1f} ms")

    await client.disconnect()

asyncio.run(main())