# utils/tor_downloader.py

import os
import math
import requests
from urllib.parse import urlparse

class TorDownloader:
    """
    Скачивает диапазон [start..end] байт (или весь файл, если без диапазона)
    через локальный Tor SOCKS5 на заданном порту, разбивая на `circuits` чанков.
    """
    def __init__(self, tor_port: int = 9050, circuits: int = 20, allow_http: bool = False, quiet: bool = False):
        self.proxies = {
            "http":  f"socks5h://127.0.0.1:{tor_port}",
            "https": f"socks5h://127.0.0.1:{tor_port}",
        }
        self.circuits = circuits
        self.allow_http = allow_http
        self.quiet = quiet

    @staticmethod
    def _head(url, proxies, allow_http):
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError("Unsupported URL scheme")
        if scheme == "http" and not allow_http:
            raise ValueError("HTTP not allowed")
        r = requests.head(url, allow_redirects=True, proxies=proxies, timeout=30)
        r.raise_for_status()
        size = int(r.headers.get("Content-Length", 0))
        if size <= 0:
            raise RuntimeError("Cannot determine file size")
        return size

    def download_range(self, url: str, filepath: str, start: int = None, end: int = None, force: bool = False):
        """
        Если start/end указаны — скачиваем именно этот диапазон,
        иначе — весь файл.
        Записываем прямо в итоговый файл по смещению start.
        """
        # определяем общий размер, если надо
        total = None
        if start is None or end is None:
            total = self._head(url, self.proxies, self.allow_http)
            start, end = 0, total - 1

        # подготовка файла
        dirpath = os.path.dirname(filepath)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        if not os.path.exists(filepath) or force:
            # если файл новый или force — создаём/перезаписываем на полный размер
            if total is None:
                # нужно узнать размер для truncate
                total = self._head(url, self.proxies, self.allow_http)
            with open(filepath, "wb") as f:
                f.truncate(total)

        # разбиваем диапазон на circuits чанков
        length = end - start + 1
        chunk_size = math.ceil(length / self.circuits)
        ranges = []
        for i in range(self.circuits):
            s = start + i * chunk_size
            e = min(s + chunk_size - 1, end)
            if s <= e:
                ranges.append((s, e))

        if not self.quiet:
            print(f"[Tor:{self.proxies['http'].split(':')[-1]}] Downloading bytes {start}-{end}")

        # скачиваем чанки подряд (можно легко обернуть в ThreadPool для параллели)
        for s, e in ranges:
            headers = {"Range": f"bytes={s}-{e}"}
            r = requests.get(url, headers=headers, stream=True, proxies=self.proxies, timeout=60)
            r.raise_for_status()
            with open(filepath, "r+b") as f:
                f.seek(s)
                for buf in r.iter_content(1024 * 16):
                    f.write(buf)

        if not self.quiet:
            print(f"[Tor:{self.proxies['http'].split(':')[-1]}] Segment {start}-{end} done")
