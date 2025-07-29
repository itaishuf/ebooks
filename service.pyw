import base64
import os
import sys
import asyncio

from fastapi import FastAPI, Query, HTTPException
from download_flow import ebook_download

# redirect console output
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

app = FastAPI()
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"


@app.get('/')
async def post(goodreads_url: str, kindle_mail: str):
    try:
        await ebook_download(goodreads_url, kindle_mail)
        return "success, check your inbox for confirmation"
    except Exception as e:
        raise HTTPException(status_code=400,
                            detail=f"Error processing data: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=19191)
