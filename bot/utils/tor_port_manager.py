import asyncio
import time

proxy_port_state = {
    "banned": {},  # port: timestamp
    "good": [],
    "index": 0
}

async def ban_port(port, duration=600):
    proxy_port_state["banned"][port] = time.time() + duration
    if port in proxy_port_state["good"]:
        proxy_port_state["good"].remove(port)

async def get_next_good_port():
    ports = proxy_port_state["good"]
    if not ports:
        return None
    proxy_port_state["index"] = (proxy_port_state["index"] + 1) % len(ports)
    return ports[proxy_port_state["index"]]

async def unban_ports_forever():
    while True:
        now = time.time()
        for port in list(proxy_port_state["banned"].keys()):
            if proxy_port_state["banned"][port] < now:
                proxy_port_state["banned"].pop(port, None)
                if port not in proxy_port_state["good"]:
                    proxy_port_state["good"].append(port)
        await asyncio.sleep(5)

async def normalize_all_ports_forever_for_url(
    url,
    proxy_ports,
    tor_manager,
    timeout_seconds=5,
    max_acceptable_response_time=5.0,
    min_speed_kbps=300
):
    import aiohttp
    import time
    from aiohttp_socks import ProxyConnector

    port_speed_log = {}

    print(f"\n🔁 Бесконечная проверка {len(proxy_ports)} Tor-портов на доступ к: {url}\n")

    async def normalize_port_forever(index, port):
        attempt = 0
        while True:
            attempt += 1
            try:
                connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                headers = {
                    'User-Agent': 'Mozilla/5.0',
                    'Accept': '*/*',
                    'Referer': 'https://www.youtube.com/'
                }

                print(f"[{port}] 🧪 Попытка #{attempt} — HEAD-запрос...")

                async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
                    start_time = time.time()
                    async with session.head(url, allow_redirects=False) as resp:
                        elapsed = time.time() - start_time
                        content_length = resp.headers.get("Content-Length")

                        if resp.status in [403, 429]:
                            print(f"[{port}] 🚫 Статус {resp.status} — IP забанен ({elapsed:.2f}s)")
                            await tor_manager.renew_identity(index)
                            await ban_port(port)
                            continue

                        if 500 <= resp.status < 600:
                            print(f"[{port}] ❌ Серверная ошибка {resp.status}")
                            await tor_manager.renew_identity(index)
                            continue

                        if elapsed > max_acceptable_response_time:
                            print(f"[{port}] 🐢 Медленно: {elapsed:.2f}s")
                            await tor_manager.renew_identity(index)
                            continue

                        if content_length:
                            try:
                                content_length_bytes = int(content_length)
                                speed_kbps = (content_length_bytes / 1024) / elapsed
                                if speed_kbps < min_speed_kbps:
                                    print(f"[{port}] 🐌 Низкая скорость: {speed_kbps:.2f} KB/s")
                                    await tor_manager.renew_identity(index)
                                    continue
                            except Exception:
                                pass

                        print(f"[{port}] ✅ Успех! Статус {resp.status} | Время: {elapsed:.2f}s | Попытка #{attempt}")
                        port_speed_log[port] = elapsed
                        if port not in proxy_port_state["good"]:
                            proxy_port_state["good"].append(port)
                        return

            except Exception as e:
                print(f"[{port}] ❌ Ошибка: {e} | Попытка #{attempt}")
                await tor_manager.renew_identity(index)
                continue

    await asyncio.gather(*(normalize_port_forever(i, port) for i, port in enumerate(proxy_ports)))

    print("\n📈 Финальный отчёт по HEAD-запросам:")
    for port in sorted(port_speed_log.keys()):
        print(f"✅ Порт {port}: {port_speed_log[port]:.2f} сек")

    return list(port_speed_log.keys()), port_speed_log
