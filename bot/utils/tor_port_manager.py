import asyncio
import time
from asyncio import locks
from stem import Signal
from stem.control import Controller
from aiohttp_socks import ProxyConnector
import aiohttp

from bot.utils.log import log_action

proxy_port_state = {
    "banned": {},
    "good": [],
    "index": 0
}

control_ports = []
last_changed = {}
locks = {}

async def ban_port(port):
    if port in proxy_port_state["good"]:
        proxy_port_state["good"].remove(port)
    proxy_port_state["banned"][port] = True  # Просто помечаем порт как заблокированный

async def renew_identity(socks_port, delay_between=10):
    control_port = socks_port + 1
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
                log_action(f"\u267b\ufe0f IP обновлён через контрол порт {control_port}")
        except Exception as e:
            await ban_port(socks_port)
            log_action(f"❌ Ошибка при NEWNYM для порта {control_port}: {e}")

async def get_next_good_port():
    ports = proxy_port_state["good"]
    if not ports:
        return None
    proxy_port_state["index"] = (proxy_port_state["index"] + 1) % len(ports)
    return ports[proxy_port_state["index"]]

import aiohttp
from aiohttp_socks import ProxyConnector
import time
import asyncio
from bot.utils.log import log_action

async def try_until_successful_connection(
    index, port, url,
    timeout_seconds=15,
    max_attempts=5,
    min_speed_kbps=2000,
    pre_ip_renew_delay=2,
    post_renew_delay=3
):
    attempt = 0
    range_bytes = 10240 * 1024  # 512 KB

    while attempt < max_attempts:
        attempt += 1
        try:
            connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{port}')
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Accept': '*/*',
                'Referer': 'https://www.youtube.com/',
                'Range': f'bytes=0-{range_bytes - 1}'
            }

            log_action(f"[{port}] 🧪 Попытка #{attempt} — измерение реальной скорости...")

            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                start_time = time.time()
                async with session.get(url, headers=headers, allow_redirects=False) as resp:
                    elapsed = time.time() - start_time
                    data = await resp.read()
                    size_kb = len(data) / 1024
                    speed_kbps = size_kb / elapsed
                    speed_mbps = speed_kbps / 1024

                    log_action(f"[{port}] ⚡️ Реальная скорость: {speed_kbps:.2f} KB/s ({speed_mbps:.2f} MB/s) за {elapsed:.2f} сек:")

                    if resp.status in [403, 429]:
                        log_action(f"[{port}] 🚫 Статус {resp.status} — IP забанен")
                        await renew_identity(port)
                        await asyncio.sleep(post_renew_delay)
                        return None

                    if 500 <= resp.status < 600:
                        log_action(f"[{port}] ❌ Серверная ошибка {resp.status}")
                        await renew_identity(port)
                        await asyncio.sleep(post_renew_delay)
                        continue

                    if speed_kbps < min_speed_kbps:
                        log_action(f"[{port}] 🐌 Низкая скорость: {speed_kbps:.2f} KB/s (< {min_speed_kbps})")
                        await renew_identity(port)
                        await asyncio.sleep(post_renew_delay)
                        continue

                    log_action(f"[{port}] ✅ Успех! Статус {resp.status} | Попытка #{attempt}")
                    return speed_kbps

        except Exception as e:
            log_action(f"[{port}] ❌ Ошибка: {type(e).__name__}: {e} | Попытка #{attempt}")
            await renew_identity(port)
            await asyncio.sleep(post_renew_delay)

    log_action(f"[{port}] ❌ Все {max_attempts} попыток неудачны — смена IP:")
    await renew_identity(port)
    await asyncio.sleep(post_renew_delay)
    return None


normalizing_ports = set()

async def normalize_all_ports_forever_for_url(
    url,
    proxy_ports,
    timeout_seconds=5,
    max_acceptable_response_time=5.0,
    min_speed_kbps=2000,
    max_parallel=10,
    sequential=True
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

    if sequential:
        for i, port in enumerate(proxy_ports):
            if port in normalizing_ports:
                continue
            normalizing_ports.add(port)
            await normalize_port_forever(i, port)
    else:
        tasks = []
        for i, port in enumerate(proxy_ports):
            if port in normalizing_ports:
                continue
            normalizing_ports.add(port)
            tasks.append(asyncio.create_task(normalize_port_forever(i, port)))
        await asyncio.gather(*tasks)

    good_count = len(proxy_port_state["good"])
    percent_good = good_count / total_ports
    log_action(f"\n♻️ Результат: {good_count}/{total_ports} портов рабочие ({percent_good*100:.1f}%)")
    for port in sorted(port_speed_log):
        log_action(f"✅ Порт {port}: {port_speed_log[port]:.2f} сек")

    return list(port_speed_log.keys()), port_speed_log

normalizing_ports = set()

async def unban_ports_forever(url, max_parallel=5, parallel=False, timeout_seconds=5,
    max_acceptable_response_time=5.0,
    min_speed_kbps=2000):
    semaphore = asyncio.Semaphore(max_parallel)

    async def retry_until_success(port):
        async with semaphore:
            while True:
                log_action(f"[{port}] 🔁 Начинается процесс разбана...")
                try:
                    start = time.time()
                    elapsed = await try_until_successful_connection(
                        index=0,
                        port=port,
                        url=url,
                        timeout_seconds=timeout_seconds,
                        max_acceptable_response_time=max_acceptable_response_time,
                        min_speed_kbps=min_speed_kbps
                    )
                    log_action(f"[{port}] ✅ Разбанен за {elapsed:.2f}s")
                    if port not in proxy_port_state["good"]:
                        proxy_port_state["good"].append(port)
                    log_action(f"[{port}] ✅ Разбанен за {elapsed:.2f}s")
                    normalizing_ports.discard(port)
                    break
                except Exception as e:
                    log_action(f"[{port}] ❌ Ошибка при разблокировке: {e}")
                    await asyncio.sleep(2)

    async def loop_forever():
        while True:
            to_unban = [port for port in proxy_port_state["banned"]]

            for port in to_unban:
                if port in normalizing_ports:
                    continue
                proxy_port_state["banned"].pop(port, None)
                normalizing_ports.add(port)
                log_action(f"[{port}] 🔎 Попытка разблокировки...")

                if parallel:
                    log_action(f"[{port}] 🔁 Процесс разбана запускается в фоновом режиме.")
                    asyncio.create_task(retry_until_success(port))
                else:
                    log_action(f"[{port}] 🔁 Процесс разбана выполняется последовательно.")
                    await retry_until_success(port)

            await asyncio.sleep(5)

    # Запускаем бесконечную задачу в фоне
    asyncio.create_task(loop_forever())