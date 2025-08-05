import os
import logging
import sys
import pathlib

from fastapi import FastAPI, HTTPException

from download_flow import ebook_download


# redirect console output so script will run with pythonw
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

logger = logging.getLogger(__name__)
log_path = pathlib.WindowsPath(rf'{os.getenv("APPDATA")}\ebookarr\books.log').absolute()
logging.basicConfig(filename=str(log_path),
                    format='%(asctime)s [%(filename)s:%(lineno)s - %(funcName)s() ] %(message)s', level=logging.DEBUG)

app = FastAPI()
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"


@app.get('/')
async def handler(goodreads_url: str, kindle_mail: str):
    try:
        await ebook_download(goodreads_url, kindle_mail)
        return "success, check your inbox for confirmation"
    except Exception as e:
        logger.info(e)
        raise HTTPException(status_code=400,
                            detail=f"Error processing data: {goodreads_url}, {kindle_mail}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=19191)
