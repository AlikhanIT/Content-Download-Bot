from asyncio import Lock

# In-memory cache and lock for thread safety
cache = {}
cache_lock = Lock()

async def save_to_cache(video_id, download_type, quality, file_id):
    key = (video_id, download_type, quality)
    async with cache_lock:
        cache[key] = file_id

async def get_from_cache(video_id, download_type, quality):
    key = (video_id, download_type, quality)
    async with cache_lock:
        return cache.get(key)

async def remove_from_cache(video_id, download_type, quality):
    key = (video_id, download_type, quality)
    async with cache_lock:
        cache.pop(key, None)
