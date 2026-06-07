import os
from dotenv import load_dotenv

load_dotenv()

CHATGPT_URL = "https://chatgpt.com"
GENERATION_TIMEOUT = 300
TEMP_DOWNLOADS = os.getenv("TEMP_DOWNLOADS", r"W:\_python\APIPROXY\temp_downloads")
TYPING_DELAY = (0.01, 0.05)

# Авто-саммари
AUTO_SUMMARY_ENABLED = os.getenv("AUTO_SUMMARY_ENABLED", "true").lower() == "true"
TOKEN_LIMIT = int(os.getenv("TOKEN_LIMIT", "200000"))
SUMMARY_DIR = os.getenv("SUMMARY_DIR", r"W:\_python\APIPROXY\temp\summaries")
DUMP_MAX_AGE_DAYS = int(os.getenv("DUMP_MAX_AGE_DAYS", "7"))

# Пул профилей
PROFILES = [p.strip() for p in os.getenv("PROFILES", "Profile 2").split(",") if p.strip()]
PROFILES_DIR = os.getenv("PROFILES_DIR", r"W:\_python\APIPROXY\profiles")
WORK_DIR = os.getenv("WORK_DIR", r"W:\_python\APIPROXY\work")

# Профили, которые НЕ сворачиваются на старте (для визуального контроля при тестах).
# Override через env: NO_MINIMIZE_PROFILES="Profile 2,Profile_Fixed"
NO_MINIMIZE_PROFILES = [p.strip() for p in os.getenv("NO_MINIMIZE_PROFILES", "Profile 2,Profile 4,Profile_Fixed").split(",") if p.strip()]

# Сетевой биндинг
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.getenv("BIND_PORT", "47821"))

# Опциональная авторизация (Bearer token). Пусто = без авторизации.
API_KEY = os.getenv("API_KEY", "")
