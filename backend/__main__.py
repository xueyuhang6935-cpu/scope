import os

import uvicorn
from dotenv import load_dotenv

from .main import ROOT

if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    reload = "--reload" in __import__("sys").argv
    uvicorn.run("backend.main:create_app", factory=True, host=os.getenv("AUTH_HOST", "0.0.0.0"),
                port=int(os.getenv("AUTH_PORT", "3002")), reload=reload,
                reload_dirs=[str(ROOT / "backend")] if reload else None)
