import asyncio
import os
import subprocess
import uuid

from bot.utils.log import log_action


class YtDlpDownloader:
    _instance = None  # Singleton экземпляр

    def __new__(cls, max_threads=8):
        if cls._instance is None:
            cls._instance = super(YtDlpDownloader, cls).__new__(cls)
            cls._instance.max_threads = max_threads
        return cls._instance

    async def download(self, url, download_type="video", quality="720", output_dir="downloads"):
        os.makedirs(output_dir, exist_ok=True)
        random_name = str(uuid.uuid4())
        output_file = os.path.join(output_dir, f"{random_name}.mp4" if download_type == "video" else f"{random_name}.mp3")

        if download_type == "video":
            format_option = f"bestvideo[height={quality}]+bestaudio[abr<=128]/best[height={quality}]"
        else:
            format_option = "bestaudio[abr<=128]/best"

        command = [
            "yt-dlp",
            "-f", format_option,
            "-N", str(self.max_threads),  # Установим 8 потоков
            "--merge-output-format", "mp4",
            "-o", output_file,
            url
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            log_action(f"Скачивание завершено: {output_file}")
            return output_file
        else:
            log_action(f"Ошибка скачивания: {stderr.decode()}")
            return None
