import pytest
import asyncio
import os
import tempfile
import shutil
import subprocess
from unittest import mock
from bot.utils.YtDlpDownloader import YtDlpDownloader

@pytest.fixture(scope="module")
def downloader():
    return YtDlpDownloader()

@pytest.mark.asyncio
async def test_prepare_file_paths_audio(downloader):
    result = await downloader._prepare_file_paths("audio")
    assert "audio" in result
    assert result["audio"].endswith(".m4a")

@pytest.mark.asyncio
async def test_prepare_file_paths_video(downloader):
    result = await downloader._prepare_file_paths("video")
    assert all(k in result for k in ["video", "audio", "output"])
    assert result["video"].endswith("_video.mp4")
    assert result["audio"].endswith("_audio.m4a")
    assert result["output"].endswith(".mp4")

@pytest.mark.asyncio
async def test_merge_files_success(downloader):
    # Проверим, доступен ли ffmpeg
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("ffmpeg не установлен в системе")

    with tempfile.TemporaryDirectory() as tmpdir:
        video = os.path.join(tmpdir, "video.mp4")
        audio = os.path.join(tmpdir, "audio.m4a")
        output = os.path.join(tmpdir, "final.mp4")

        with open(video, "wb") as f:
            f.write(os.urandom(1024))
        with open(audio, "wb") as f:
            f.write(os.urandom(1024))

        file_paths = {"video": video, "audio": audio, "output": output}
        result = await downloader._merge_files(file_paths)

        assert os.path.exists(result)
        assert os.path.getsize(result) > 0

@pytest.mark.asyncio
async def test_cleanup_temp_files(downloader):
    with tempfile.TemporaryDirectory() as tmpdir:
        vpath = os.path.join(tmpdir, "v.mp4")
        apath = os.path.join(tmpdir, "a.m4a")
        with open(vpath, "w") as f:
            f.write("video")
        with open(apath, "w") as f:
            f.write("audio")

        file_paths = {"video": vpath, "audio": apath}
        await downloader._cleanup_temp_files(file_paths)
        assert not os.path.exists(vpath)
        assert not os.path.exists(apath)

@pytest.mark.asyncio
async def test_queue_and_worker(downloader):
    async def fake_process_download(url, download_type, quality, progress_msg):
        return f"{url}_{quality}.mp4"

    with mock.patch.object(downloader, '_process_download', side_effect=fake_process_download):
        result = await downloader.download("https://youtube.com/fake", "video", "720")
        assert result.endswith("720.mp4")

