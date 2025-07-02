#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import uuid
import subprocess
import asyncio
import time
from functools import cached_property
from contextlib import asynccontextmanager

import aiofiles
import aiohttp
# aiohttp_socks.ProxyConnector is no longer directly used for downloads,
# as tor-dl handles the proxying itself.
from fake_useragent import UserAgent
from tqdm import tqdm

# Assuming these are available in the bot/utils directory
from bot.utils.tor_port_manager import ban_port, get_next_good_port
from bot.utils.video_info import get_video_info_with_cache, extract_url_from_info
from bot.utils.log import log_action


class PortPool:
    """
    Manages the acquisition and release of Tor SOCKS5 ports for concurrent downloads.
    It integrates with a global port manager (`get_next_good_port`) to ensure
    only healthy ports are used. The semaphore limits the total number of
    concurrent download blocks active at any given time.
    """

    def __init__(self, max_concurrent_downloads):
        # The semaphore limits the total number of concurrent download blocks
        # that can be active simultaneously, preventing overwhelming the system.
        self._sem = asyncio.Semaphore(max_concurrent_downloads)

    @asynccontextmanager
    async def acquire(self):
        """
        Acquires a semaphore slot and retrieves a good Tor port from the global manager.
        """
        await self._sem.acquire()
        port = None
        try:
            # Get a good port from the global Tor port manager.
            # This function is assumed to handle port cycling and health checks.
            port = await get_next_good_port()
            yield port
        finally:
            # Release the semaphore slot, allowing another download block to start.
            # The port itself is not "returned" to this pool, as its global state
            # is managed by `get_next_good_port` and `ban_port`.
            self._sem.release()


class YtDlpDownloader:
    _instance = None
    DOWNLOAD_DIR = '/downloads'
    QUALITY_ITAG_MAP = {
        "144": "160", "240": "133", "360": "134", "480": "135",
        "720": "136", "1080": "137", "1440": "264", "2160": "266"
    }
    DEFAULT_VIDEO_ITAG = "243"
    DEFAULT_AUDIO_ITAG = "249"
    MAX_RETRIES = 10
    # Path to the tor-dl executable.
    # Based on your previous input, it seems tor-dl is located at C:\Progs\tor-dl\tor-dl.exe
    # It's crucial to provide the full, correct path to the executable.
    TOR_DL_EXECUTABLE = r'C:\Progs\tor-dl\tor-dl.exe'  # Updated to full path

    # --- NEW: Define min and max block sizes for dynamic splitting ---
    MIN_BLOCK_SIZE = 2 * 1024 * 1024  # 2 MB
    MAX_BLOCK_SIZE = 50 * 1024 * 1024  # 50 MB

    # --- END NEW ---

    def __new__(cls, max_threads=8, max_queue_size=20):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize(max_threads, max_queue_size)
            cls._instance._ensure_download_dir()
        return cls._instance

    def _initialize(self, max_threads, max_queue_size):
        """
        Initializes the downloader's internal state.
        """
        self.max_threads = max_threads
        self.queue = asyncio.Queue(maxsize=max_queue_size)
        self.is_running = False
        self.active_tasks = set()
        # Initialize PortPool with max_threads to limit concurrent download blocks.
        self.port_pool = PortPool(max_concurrent_downloads=max_threads)

        # --- NEW: Check for tor-dl executable existence at initialization ---
        if not os.path.exists(self.TOR_DL_EXECUTABLE):
            raise FileNotFoundError(
                f"Error: tor-dl executable not found at '{self.TOR_DL_EXECUTABLE}'. "
                "Please ensure the path is correct and the file exists."
            )
        if not os.path.isfile(self.TOR_DL_EXECUTABLE):
            raise FileNotFoundError(
                f"Error: '{self.TOR_DL_EXECUTABLE}' is not a file. "
                "Please ensure the path points to the tor-dl executable."
            )
        # Optional: Check if it's executable (might vary by OS/permissions)
        # if not os.access(self.TOR_DL_EXECUTABLE, os.X_OK):
        #     log_action(f"Warning: tor-dl executable at '{self.TOR_DL_EXECUTABLE}' might not be executable. Check permissions.")
        # --- END NEW ---

    def _ensure_download_dir(self):
        """
        Ensures the download directory exists.
        """
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        log_action(f"📂 Папка для загрузки: {self.DOWNLOAD_DIR}")

    @cached_property
    def user_agent(self):
        """
        Provides a cached UserAgent instance for HTTP requests.
        """
        return UserAgent()

    async def start_workers(self):
        """
        Starts the background worker tasks that process download requests from the queue.
        """
        if not self.is_running:
            self.is_running = True
            for _ in range(self.max_threads):
                task = asyncio.create_task(self._worker())
                self.active_tasks.add(task)
                task.add_done_callback(self.active_tasks.discard)

    async def download(self, url, download_type="video", quality="480", progress_msg=None):
        """
        Adds a download request to the queue and waits for its completion.
        This is the main public method to initiate a download.

        Args:
            url (str): The URL of the video/audio to download.
            download_type (str): "video" for video+audio, "audio" for audio only.
            quality (str): Desired video quality (e.g., "480", "720").
            progress_msg (Any): Optional progress message handler.

        Returns:
            str: The path to the downloaded file.
        """
        start_time = time.time()
        await self.start_workers()  # Ensure workers are running
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        # Put the download request into the queue
        await self.queue.put((url, download_type, quality, future, progress_msg))
        result = await future  # Wait for the download to complete

        try:
            size = os.path.getsize(result)
            duration = time.time() - start_time
            avg = size / duration if duration > 0 else 0
            log_action(f"📊 Finished: {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
        except Exception as e:
            log_action(f"⚠️ Error getting file size or calculating final stats: {e}")
            # Do not re-raise, as the download itself might have succeeded but stat calculation failed.
        return result

    async def _worker(self):
        """
        Background worker that continuously fetches and processes download tasks from the queue.
        """
        while True:
            # Get a download task from the queue
            url, download_type, quality, future, progress_msg = await self.queue.get()
            try:
                # Process the download
                res = await self._process_download(url, download_type, quality, progress_msg)
                future.set_result(res)  # Set the result for the waiting future
            except Exception as e:
                log_action(f"❌ Worker error processing download for {url}: {e}")
                future.set_exception(e)  # Set exception for the waiting future
            finally:
                self.queue.task_done()  # Mark the task as done

    async def _process_download(self, url, download_type, quality, progress_msg):
        """
        Orchestrates the download process, either for audio only or video+audio,
        using `_download_with_tor_dl` for segmented downloads and then merging if necessary.

        Args:
            url (str): The URL of the content.
            download_type (str): "video" or "audio".
            quality (str): Desired video quality.
            progress_msg (Any): Optional progress message handler.

        Returns:
            str: The path to the final downloaded file.
        """
        start_proc = time.time()
        file_paths = await self._prepare_file_paths(download_type)
        result = None
        info = None  # Initialize info outside try block for finally block access

        try:
            # Get video information (e.g., available ITAGs, stream URLs)
            info = await get_video_info_with_cache(url)

            if download_type == "audio":
                # Extract audio URL and download audio only
                audio_url = await extract_url_from_info(info, [self.DEFAULT_AUDIO_ITAG])
                result = await self._download_with_tor_dl(audio_url, file_paths['audio'], 'audio', progress_msg)
            else:
                # For video download, extract both video and audio URLs
                itag = self.QUALITY_ITAG_MAP.get(str(quality), self.DEFAULT_VIDEO_ITAG)
                video_url, audio_url = await asyncio.gather(
                    extract_url_from_info(info, [itag]),
                    extract_url_from_info(info, [self.DEFAULT_AUDIO_ITAG])
                )
                # Download video and audio streams concurrently using tor-dl
                await asyncio.gather(
                    self._download_with_tor_dl(video_url, file_paths['video'], 'video', progress_msg),
                    self._download_with_tor_dl(audio_url, file_paths['audio'], 'audio', progress_msg)
                )
                # Merge the downloaded video and audio streams
                result = await self._merge_files(file_paths)
            return result
        except Exception as e:
            log_action(f"❌ Error in _process_download for {url}: {e}")
            raise  # Re-raise the exception to be caught by the worker

        finally:
            # Log processing statistics
            try:
                size = os.path.getsize(result) if result and os.path.exists(result) else 0
                duration = time.time() - start_proc
                avg = size / duration if duration > 0 else 0
                log_action(
                    f"📈 Process: {download_type.upper()} {size / 1024 / 1024:.2f} MB in {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")
            except Exception as e:
                log_action(f"⚠️ Error calculating process stats: {e}")

            # Clean up temporary files if it was a video download (and info was successfully retrieved)
            if download_type != 'audio' and info:
                await self._cleanup_temp_files(file_paths)

    async def _prepare_file_paths(self, download_type):
        """
        Generates unique file paths for temporary and final download files.

        Args:
            download_type (str): "video" or "audio".

        Returns:
            dict: A dictionary containing paths for 'output', 'video' (if applicable), and 'audio'.
        """
        rnd = uuid.uuid4()
        base = {'output': os.path.join(self.DOWNLOAD_DIR, f"{rnd}.mp4")}
        if download_type == 'audio':
            # For audio-only downloads, the final output is the .m4a file itself.
            base['audio'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}.m4a")
            base['output'] = base['audio']
        else:
            # For video downloads, separate temporary files for video and audio.
            base['video'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}_video.mp4")
            base['audio'] = os.path.join(self.DOWNLOAD_DIR, f"{rnd}_audio.m4a")
        return base

    async def _merge_files(self, file_paths):
        """
        Merges separate video and audio files into a single MP4 using FFmpeg.

        Args:
            file_paths (dict): Dictionary containing paths to 'video', 'audio', and 'output' files.

        Returns:
            str: The path to the merged output file.

        Raises:
            FileNotFoundError: If input video or audio files are missing.
            subprocess.CalledProcessError: If FFmpeg fails to merge the files.
        """
        log_action("🔄 Merging видео и аудио...")
        # Verify that input files exist before attempting to merge
        if not os.path.exists(file_paths['video']):
            raise FileNotFoundError(f"Video file not found for merging: {file_paths['video']}")
        if not os.path.exists(file_paths['audio']):
            raise FileNotFoundError(f"Audio file not found for merging: {file_paths['audio']}")

        cmd = [
            'ffmpeg', '-i', file_paths['video'], '-i', file_paths['audio'],
            '-c:v', 'copy', '-c:a', 'copy', '-map', '0:v:0', '-map', '1:a:0',
            '-y', file_paths['output']  # -y to overwrite output file if it exists
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()  # Wait for FFmpeg to complete

        if proc.returncode != 0:
            log_action(f"❌ FFmpeg error (Return Code {proc.returncode}): {err.decode()}")
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
        log_action(f"✅ Output: {file_paths['output']}")
        return file_paths['output']

    async def _cleanup_temp_files(self, file_paths):
        """
        Removes temporary video and audio files after merging or if only audio was downloaded.

        Args:
            file_paths (dict): Dictionary containing paths to files that might need cleanup.
        """
        files_to_remove = []
        if 'video' in file_paths and os.path.exists(file_paths['video']):
            files_to_remove.append(file_paths['video'])
        if 'audio' in file_paths and os.path.exists(file_paths['audio']):
            # Only remove the audio file if it's a temporary file and not the final output itself.
            if file_paths.get('output') != file_paths['audio']:
                files_to_remove.append(file_paths['audio'])

        for fp in files_to_remove:
            try:
                os.remove(fp)
                log_action(f"🧹 Removed temp: {fp}")
            except OSError as e:
                log_action(f"⚠️ Failed to remove temp file {fp}: {e}")

    async def _download_with_tor_dl(self, url, filename, media_type, progress_msg):
        """
        Downloads a file (or a stream) using `tor-dl`, splitting it into multiple parts
        and downloading them concurrently via different Tor ports.

        Args:
            url (str): The direct URL of the video/audio stream.
            filename (str): The path where the final downloaded file should be saved.
            media_type (str): A descriptive string (e.g., "video", "audio") for logging.
            progress_msg (Any): Optional progress message handler.

        Returns:
            str: The path to the downloaded file.

        Raises:
            Exception: If file size cannot be determined or all download attempts fail.
            FileNotFoundError: If a downloaded part file is missing during concatenation.
        """
        headers = {'User-Agent': self.user_agent.chrome}
        timeout = aiohttp.ClientTimeout(total=None)  # No total timeout for initial HEAD request

        # 1) Get total file size using a HEAD request. This is crucial for tqdm and block calculation.
        total_size = 0
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as sess:
                async with sess.head(url, allow_redirects=True) as r:
                    r.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
                    total_size = int(r.headers.get('Content-Length', 0))
            if total_size <= 0:
                raise Exception("Failed to get file size from Content-Length header. It's zero or missing.")
        except Exception as e:
            log_action(f"❌ Error getting total size for {filename}: {e}")
            raise  # Re-raise to stop download if size cannot be determined

        # --- UPDATED: Dynamically divide the file into blocks for parallel download. ---
        # Aim to have at least `max_threads` blocks, but respect min/max block sizes.

        # Calculate a base block size by dividing total_size by max_threads.
        # This ensures that if the file is large enough, we utilize all threads.
        calculated_block_size = total_size // self.max_threads if self.max_threads > 0 else total_size

        # Clamp the calculated block size between MIN_BLOCK_SIZE and MAX_BLOCK_SIZE
        block_size = max(self.MIN_BLOCK_SIZE, min(calculated_block_size, self.MAX_BLOCK_SIZE))

        # Ensure block_size is not zero for very small files, and is at least 1 if total_size > 0
        if total_size > 0 and block_size == 0:
            block_size = self.MIN_BLOCK_SIZE  # Fallback for extremely small files if calculation yields 0

        # If the total size is smaller than the calculated block size,
        # set block_size to total_size to avoid downloading empty blocks.
        if total_size > 0 and block_size > total_size:
            block_size = total_size

        blocks = []
        current_start_byte = 0
        while current_start_byte < total_size:
            current_end_byte = min(current_start_byte + block_size - 1, total_size - 1)
            blocks.append((current_start_byte, current_end_byte))
            current_start_byte = current_end_byte + 1

        if not blocks and total_size == 0:
            log_action(f"⚠️ No blocks to download for {filename} (file size is 0). Creating empty file.")
            async with aiofiles.open(filename, 'wb') as f:
                pass  # Create an empty file
            return filename
        elif not blocks and total_size > 0:
            # This case should ideally not happen if total_size > 0
            raise Exception(f"Failed to create download blocks for file size {total_size}.")
        # --- END UPDATED ---

        # 3) Initialize progress bar.
        pbar = tqdm(total=total_size, unit='B', unit_scale=True, desc=media_type.upper(), dynamic_ncols=True)
        t0 = time.time()  # Start time for overall download

        async def download_block_part(start, end, block_url, part_filename):
            """
            Downloads a specific byte range of a file using `tor-dl`.
            Retries with a new Tor port if the download fails.

            Args:
                start (int): Start byte of the segment.
                end (int): End byte of the segment.
                block_url (str): The URL to download from.
                part_filename (str): The temporary filename for this segment.

            Raises:
                subprocess.CalledProcessError: If `tor-dl` fails after max retries.
                FileNotFoundError: If the tor-dl executable is not found.
            """
            attempts = 0
            while attempts < self.MAX_RETRIES:
                attempts += 1
                current_port = None
                try:
                    # Acquire a port from the PortPool (which gets it from get_next_good_port)
                    async with self.port_pool.acquire() as port:
                        current_port = port
                        log_action(
                            f"⬇️ Downloading {media_type} part {start}-{end} via port {port} (Attempt {attempts}/{self.MAX_RETRIES})")

                        # Construct the tor-dl command
                        # Example: ./tor-dl -p 9052 -min 2000000 -max 90000000 -n part4.mp4 "https://..."
                        cmd = [
                            self.TOR_DL_EXECUTABLE,
                            '-p', str(port),
                            '-min', str(start),
                            '-max', str(end),
                            '-n', part_filename,
                            block_url
                        ]
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout, stderr = await proc.communicate()  # Wait for tor-dl to finish

                        if proc.returncode != 0:
                            error_msg = stderr.decode().strip()
                            log_action(
                                f"❌ tor-dl failed for {part_filename} (Port {port}, Range {start}-{end}): Return Code {proc.returncode}, Error: {error_msg}")
                            await ban_port(port)  # Ban the port if tor-dl indicates an error
                            raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
                        else:
                            log_action(f"✅ tor-dl success for {part_filename} (Port {port}, Range {start}-{end})")
                            pbar.update(end - start + 1)  # Update progress bar on successful block download
                            return  # Block downloaded successfully, exit loop
                except FileNotFoundError as e:
                    # Specific error if the tor-dl executable itself is not found
                    log_action(f"❌ Critical Error: tor-dl executable not found at '{self.TOR_DL_EXECUTABLE}'. "
                               f"Please verify the path and ensure tor-dl.exe is present and accessible. Error: {e}")
                    raise  # Re-raise immediately as this is a configuration error, not a transient network issue.
                except Exception as e:
                    log_action(f"⚠️ Error downloading block {start}-{end} with port {current_port}: {e}")
                    if attempts >= self.MAX_RETRIES:
                        log_action(f"❌ Max retries reached for block {start}-{end}. Giving up.")
                        raise  # Re-raise if max retries are exhausted
                    await asyncio.sleep(1)  # Small delay before retrying

        # 4) Launch tasks for all blocks concurrently.
        download_tasks = []
        for i, (s, e) in enumerate(blocks):
            # Create a unique temporary filename for each part.
            part_path = os.path.join(self.DOWNLOAD_DIR, f"{os.path.basename(filename)}.part{i:03d}-{s}-{e}")
            download_tasks.append(asyncio.create_task(download_block_part(s, e, url, part_path)))

        try:
            await asyncio.gather(*download_tasks)  # Wait for all download tasks to complete
        except Exception as e:
            log_action(f"❌ One or more download blocks failed: {e}")
            raise  # Re-raise if any block failed, stopping the overall download

        pbar.close()  # Close the progress bar

        # 5) Concatenate downloaded parts into the final file.
        log_action(f"🧩 Concatenating {len(blocks)} parts into {filename}...")
        async with aiofiles.open(filename, 'wb') as out_file:
            for i, (s, e) in enumerate(blocks):
                part_path = os.path.join(self.DOWNLOAD_DIR, f"{os.path.basename(filename)}.part{i:03d}-{s}-{e}")
                if not os.path.exists(part_path):
                    log_action(f"❌ Missing part file: {part_path}. Cannot complete download.")
                    raise FileNotFoundError(f"Part file not found: {part_path}")
                async with aiofiles.open(part_path, 'rb') as in_part_file:
                    chunk = await in_part_file.read()
                    await out_file.write(chunk)
                os.remove(part_path)  # Clean up the temporary part file after it's written to the final file
                log_action(f"🗑️ Removed part: {part_path}")

        # 6) Final logging of download statistics.
        duration = time.time() - t0
        avg = total_size / duration if duration > 0 else 0
        log_action(
            f"✅ {media_type.upper()} {total_size / 1024 / 1024:.2f}MB за {duration:.2f}s ({avg / 1024 / 1024:.2f} MB/s)")

        return filename


def safe_log(msg):
    """
    Utility function for logging messages safely, especially when tqdm is active.
    """
    tqdm.write(msg)  # Writes message without interfering with tqdm's output
    log_action(msg)  # Logs the action to the main log
