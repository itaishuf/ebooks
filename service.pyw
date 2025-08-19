import logging
import os
import pathlib
import sys

from fastapi import FastAPI, HTTPException

from download_flow import ebook_download


PORT = 19193
LOG_PATH = pathlib.WindowsPath(rf'{os.getenv("APPDATA")}\ebookarr\test_books.log').absolute()


# redirect console output so script will run with pythonw
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

logging.basicConfig(filename=str(LOG_PATH), level=logging.DEBUG,
                    format='%(asctime)s, [%(filename)s:%(lineno)s - %(funcName)s()], %(levelname)s, "%(message)s"')
logger = logging.getLogger(__name__)

app = FastAPI()
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"


@app.get('/')
async def handler(goodreads_url: str, kindle_mail: str):
    try:
        await ebook_download(goodreads_url, kindle_mail)
        return "success, check your inbox for confirmation"
    except Exception as e:
        logger.error(e.with_traceback())
        raise HTTPException(status_code=400,
                            detail=f"Error processing data: {goodreads_url}, {kindle_mail}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
