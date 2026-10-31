"""Start the server: python run.py  (env: PA_DATA_DIR, PA_HOST, PA_PORT, PA_SECRET_KEY, PA_SECURE_COOKIES)."""
import os

import uvicorn

from app.main import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("PA_HOST", "127.0.0.1"), port=int(os.environ.get("PA_PORT", "8000")),
                proxy_headers=True)
