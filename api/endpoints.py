from fastapi import FastAPI, BackgroundTasks, APIRouter
from enum import Enum
from pydantic import BaseModel

app = FastAPI()
router = APIRouter()



@router.get("/ping")
async def ping():
    return {"pong": True}

app.include_router(router)
