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
    proxy_port_state["banned"][port] = time.time()  # можно вообще убрать, если не хочешь логировать
    if port in proxy_port_state["good"]:
        proxy_port_state["good"].remove(port)

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

async def try_until_successful_connection(
    index, port, url,
    timeout_seconds=5,
    max_acceptable_response_time=5.0,
    min_speed_kbps=100,
    max_attempts=20,
    pre_ip_renew_delay=2,
    max_consecutive_slow=5
):
    attempt = 0
    slow_count = 0
    while attempt < max_attempts:
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
                    if elapsed < 0.1:
                        log_action(f"[{port}] ⚠️ Время отклика слишком маленькое ({elapsed:.3f}s), пробуем другой IP.")
                        await asyncio.sleep(pre_ip_renew_delay)
                        await renew_identity(port)
                        continue
                    content_length = resp.headers.get("Content-Length")
                    if resp.status in [403, 429]:
                        log_action(f"[{port}] 🚫 Статус {resp.status} — IP забанен ({elapsed:.2f}s)")
                        await asyncio.sleep(pre_ip_renew_delay)
                        await renew_identity(port)
                        await ban_port(port)
                        return None
                    if 500 <= resp.status < 600:
                        log_action(f"[{port}] ❌ Серверная ошибка {resp.status}")
                        await asyncio.sleep(pre_ip_renew_delay)
                        await renew_identity(port)
                        continue
                    if elapsed > max_acceptable_response_time:
                        log_action(f"[{port}] 🙒 Медленно: {elapsed:.2f}s")
                        slow_count += 1
                    else:
                        slow_count = 0
                    if content_length:
                        try:
                            content_length_bytes = int(content_length)
                            speed_kbps = (content_length_bytes / 1024) / elapsed
                            if speed_kbps < min_speed_kbps:
                                log_action(f"[{port}] 🐌 Низкая скорость: {speed_kbps:.2f} KB/s")
                                slow_count += 1
                            else:
                                slow_count = 0
                        except Exception:
                            log_action(f"[{port}] ⚠️ Ошибка при расчёте скорости, продолжаем")
                    else:
                        log_action(f"[{port}] ⚠️ Нет Content-Length — пропускаем проверку скорости.")
                    if slow_count >= max_consecutive_slow:
                        log_action(f"[{port}] 🔁 Слишком много медленных попыток подряд ({slow_count}) — смена IP через {pre_ip_renew_delay} сек.")
                        await asyncio.sleep(pre_ip_renew_delay)
                        await renew_identity(port)
                        slow_count = 0
                        continue
                    if port not in proxy_port_state["good"]:
                        proxy_port_state["good"].append(port)
                    log_action(f"[{port}] ✅ Успех! Статус {resp.status} | Время: {elapsed:.2f}s | Попытка #{attempt}")
                    return elapsed
        except Exception as e:
            log_action(f"[{port}] ❌ Ошибка: {e} | Попытка #{attempt}")
            await asyncio.sleep(pre_ip_renew_delay)
            await renew_identity(port)
            continue
    log_action(f"[{port}] ❌ Все {max_attempts} попыток неудачны — смена IP через {pre_ip_renew_delay} сек.")
    await asyncio.sleep(pre_ip_renew_delay)
    await renew_identity(port)
    return None

normalizing_ports = set()

async def normalize_all_ports_forever_for_url(
    url,
    proxy_ports,
    timeout_seconds=5,
    max_acceptable_response_time=5.0,
    min_speed_kbps=2000,
    required_percentage=0.75,
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

async def unban_ports_forever(url, max_parallel=5, parallel=False):
    semaphore = asyncio.Semaphore(max_parallel)

    async def retry_until_success(port):
        async with semaphore:
            while True:
                log_action(f"[{port}] 🔄 Повторная попытка разбана...")
                elapsed = await try_until_successful_connection(
                    index=0,
                    port=port,
                    url=url,
                    min_speed_kbps=2000
                )
                if port in proxy_port_state["good"]:
                    log_action(f"[{port}] ✅ Успешно разбанен | Время отклика: {elapsed:.2f}s")
                    normalizing_ports.discard(port)
                    break
                await asyncio.sleep(1)

    while True:
        to_unban = list(proxy_port_state["banned"].keys())

        for port in to_unban:
            if port in normalizing_ports:
                continue
            proxy_port_state["banned"].pop(port, None)
            normalizing_ports.add(port)

            if parallel:
                asyncio.create_task(retry_until_success(port))
            else:
                await retry_until_success(port)

        await asyncio.sleep(5)

