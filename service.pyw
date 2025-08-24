import logging
import os
import pathlib
import sys
import asyncio
import uuid
from typing import Dict, Any

from fastapi import FastAPI, HTTPException, Response

from download_flow import ebook_download


PORT = 19192
LOG_PATH = pathlib.WindowsPath(rf'{os.getenv("APPDATA")}\ebookarr\test_books.log').absolute()


# redirect console output so script will run with pythonw
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

logging.basicConfig(filename=str(LOG_PATH), level=logging.DEBUG,
                    format='%(asctime)s, [%(filename)s:%(lineno)s - %(funcName)s()], %(levelname)s, "%(message)s"')
logger = logging.getLogger(__name__)

app = FastAPI()
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

# In-memory progress and task store
PROGRESS: Dict[str, Dict[str, Any]] = {}
TASKS: Dict[str, asyncio.Task] = {}


@app.get('/')
async def handler(goodreads_url: str, kindle_mail: str, response: Response):
    """
    Start the ebook download job in the background and return a task id.
    Clients should poll /progress/{task_id} to update a progress bar.
    """
    task_id = str(uuid.uuid4())
    PROGRESS[task_id] = {"percent": 0, "message": "Queued", "done": False, "error": None}

    def _progress_cb(percent: int, message: str):
        PROGRESS[task_id] = {
            "percent": max(0, min(100, int(percent))),
            "message": message,
            "done": PROGRESS[task_id].get("done", False),
            "error": PROGRESS[task_id].get("error"),
        }

    async def _run():
        try:
            await ebook_download(goodreads_url, kindle_mail, progress_cb=_progress_cb)
            PROGRESS[task_id].update({"percent": 100, "message": "Done", "done": True})
        except Exception as e:
            logger.exception("Background job failed")
            PROGRESS[task_id].update({"done": True, "error": str(e)})

    TASKS[task_id] = asyncio.create_task(_run())
    response.status_code = 202
    return {"task_id": task_id, "progress_url": f"/progress/{task_id}"}


@app.get('/progress/{task_id}')
async def progress(task_id: str):
    info = PROGRESS.get(task_id)
    if not info:
        raise HTTPException(status_code=404, detail="task_id not found")
    return info


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
