import os
import sys
import uvicorn
from loguru import logger

# --- Логирование: читаемое, UTF-8, без ANSI-мусора в файле ---
logger.remove()
# Консоль: цвет только если это реальный TTY (при редиректе в файл — без ANSI)
logger.add(
    sys.stderr,
    level="INFO",
    colorize=sys.stderr.isatty(),
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}:{function}:{line}</cyan> - {message}",
    backtrace=False,
    diagnose=False,
)
# Файл: всегда чисто, UTF-8, без цвета, с ротацией
os.makedirs("logs", exist_ok=True)
logger.add(
    "logs/proxy.log",
    level="DEBUG",
    colorize=False,
    encoding="utf-8",
    rotation="10 MB",
    retention=5,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {name}:{function}:{line} - {message}",
    backtrace=False,
    diagnose=False,
)

from proxy.api_server import app

if __name__ == "__main__":
    from config import BIND_HOST, BIND_PORT
    uvicorn.run(app, host=BIND_HOST, port=BIND_PORT)
