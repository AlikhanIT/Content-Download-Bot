from motor.motor_asyncio import AsyncIOMotorClient
from bot.config import MONGO_URI

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["video_download_bot"]
cache_collection = db["file_cache"]

async def save_to_cache(video_id, download_type, quality, file_id):
    await cache_collection.update_one(
        {"video_id": video_id, "download_type": download_type, "quality": quality},
        {"$set": {"file_id": file_id}},
        upsert=True
    )

async def get_from_cache(video_id, download_type, quality):
    cached_file = await cache_collection.find_one(
        {"video_id": video_id, "download_type": download_type, "quality": quality}
    )
    return cached_file["file_id"] if cached_file else None
