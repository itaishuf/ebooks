import os
import logging
import os
import pathlib

from fastapi import FastAPI, HTTPException

from download_flow import ebook_download

# redirect console output
# sys.stdout = open(os.devnull, 'w')
# sys.stderr = open(os.devnull, 'w')

logger = logging.getLogger()
log_path = pathlib.WindowsPath(rf'{os.getenv("APPDATA")}\ebookarr\books.log').absolute()
logging.basicConfig(filename=str(log_path),
                    format='%(asctime)s %(message)s', level=logging.DEBUG)

app = FastAPI()
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"


@app.get('/')
async def ebook_download(goodreads_url: str, kindle_mail: str):
    try:
        await ebook_download(goodreads_url, kindle_mail)
        return "success, check your inbox for confirmation"
    except Exception as e:
        raise HTTPException(status_code=400,
                            detail=f"Error processing data: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    print(os.getpid())
    uvicorn.run(app, host="0.0.0.0", port=19191)
