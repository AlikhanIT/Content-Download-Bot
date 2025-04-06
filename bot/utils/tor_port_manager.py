import asyncio
import time

import stem
from stem import Signal
from stem.control import Controller
from aiohttp_socks import ProxyConnector
import aiohttp

from bot.utils.log import log_action
from bot.tor.tor_state import control_ports, last_changed, locks

proxy_port_state = {
    "banned": {},  # port: timestamp
    "good": [],
    "index": 0
}

async def ban_port(port, duration=600):
    proxy_port_state["banned"][port] = time.time() + duration
    if port in proxy_port_state["good"]:
        proxy_port_state["good"].remove(port)

async def renew_identity(index, delay_between=10):
    port = control_ports[index]
    control_port = port + 1
    now = time.time()

    if now - last_changed.get(control_port, 0) < delay_between:
        return

    lock = locks.setdefault(control_port, asyncio.Lock())
    async with lock:
        try:
            with Controller.from_port(port=control_port) as controller:
                controller.authenticate()
                controller.signal(Signal.NEWNYM)
                last_changed[control_port] = time.time()
                log_action(f"♻️ IP обновлён через контрол порт {control_port}")
        except Exception as e:
            await ban_port(port)  # баним socks-порт
            log_action(f"❌ Ошибка при NEWNYM для порта {control_port}: {e}")

async def get_next_good_port():
    ports = proxy_port_state["good"]
    if not ports:
        return None
    proxy_port_state["index"] = (proxy_port_state["index"] + 1) % len(ports)
    return ports[proxy_port_state["index"]]

async def try_until_successful_connection(index, port, url,
                                          timeout_seconds=5,
                                          max_acceptable_response_time=5.0,
                                          min_speed_kbps=300):
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

            log_action(f"[{port}] 🧪 Попытка #{attempt} — HEAD-запрос...")

            async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
                start_time = time.time()
                async with session.head(url, allow_redirects=False) as resp:
                    elapsed = time.time() - start_time
                    content_length = resp.headers.get("Content-Length")

                    if resp.status in [403, 429]:
                        log_action(f"[{port}] 🚫 Статус {resp.status} — IP забанен ({elapsed:.2f}s)")
                        await renew_identity(index)
                        await ban_port(port)
                        continue

                    if 500 <= resp.status < 600:
                        log_action(f"[{port}] ❌ Серверная ошибка {resp.status}")
                        await renew_identity(index)
                        continue

                    if elapsed > max_acceptable_response_time:
                        log_action(f"[{port}] 🐢 Медленно: {elapsed:.2f}s")
                        await renew_identity(index)
                        continue

                    if content_length:
                        try:
                            content_length_bytes = int(content_length)
                            speed_kbps = (content_length_bytes / 1024) / elapsed
                            if speed_kbps < min_speed_kbps:
                                log_action(f"[{port}] 🐌 Низкая скорость: {speed_kbps:.2f} KB/s")
                                await renew_identity(index)
                                continue
                        except Exception:
                            pass

                    log_action(f"[{port}] ✅ Успех! Статус {resp.status} | Время: {elapsed:.2f}s | Попытка #{attempt}")
                    if port not in proxy_port_state["good"]:
                        proxy_port_state["good"].append(port)
                    return elapsed
        except Exception as e:
            log_action(f"[{port}] ❌ Ошибка: {e} | Попытка #{attempt}")
            await renew_identity(index)
            continue

normalizing_ports = set()

async def unban_ports_forever(url, max_parallel=5):
    semaphore = asyncio.Semaphore(max_parallel)

    async def retry_until_success(port):
        async with semaphore:
            while True:
                log_action(f"[{port}] 🔄 Повторная попытка разбана...")
                elapsed = await try_until_successful_connection(
                    index=0,
                    port=port,
                    url=url,
                )
                if port in proxy_port_state["good"]:
                    log_action(f"[{port}] ✅ Успешно разбанен | Время отклика: {elapsed:.2f}s")
                    normalizing_ports.discard(port)
                    break
                await asyncio.sleep(1)

    while True:
        now = time.time()
        to_unban = [port for port, ts in proxy_port_state["banned"].items() if ts < now]
        for port in to_unban:
            if port in normalizing_ports:
                continue
            proxy_port_state["banned"].pop(port, None)
            normalizing_ports.add(port)
            asyncio.create_task(retry_until_success(port))
        await asyncio.sleep(5)

async def normalize_all_ports_forever_for_url(
    url,
    proxy_ports,
    timeout_seconds=5,
    max_acceptable_response_time=5.0,
    min_speed_kbps=300,
    required_percentage=0.75,
    max_parallel=10
):
    log_action(f"\n🔁 Бесконечная проверка {len(proxy_ports)} Tor-портов на доступ к: {url}\n")

    total_ports = len(proxy_ports)
    port_speed_log = {}
    normalizing_ports = set()
    semaphore = asyncio.Semaphore(max_parallel)

    async def normalize_port_forever(index, port):
        async with semaphore:
            while True:
                elapsed = await try_until_successful_connection(
                    index=index,
                    port=port,
                    url=url,
                    timeout_seconds=timeout_seconds,
                    max_acceptable_response_time=max_acceptable_response_time,
                    min_speed_kbps=min_speed_kbps
                )
                if port in proxy_port_state["good"]:
                    port_speed_log[port] = elapsed
                    break
                await asyncio.sleep(1)

    tasks = []
    for i, port in enumerate(proxy_ports):
        if port in normalizing_ports:
            continue
        normalizing_ports.add(port)
        tasks.append(asyncio.create_task(normalize_port_forever(i, port)))

    while True:
        good_count = len(proxy_port_state["good"])
        percent_good = good_count / total_ports
        log_action(f"⏱️ Прогресс нормализации: {good_count}/{total_ports} портов ({percent_good*100:.1f}%)")
        if percent_good >= required_percentage:
            break
        await asyncio.sleep(2)

    log_action("\n📈 Финальный отчёт по HEAD-запросам: ")
    for port in sorted(port_speed_log.keys()):
        log_action(f"✅ Порт {port}: {port_speed_log[port]:.2f} сек")

    return list(port_speed_log.keys()), port_speed_log
