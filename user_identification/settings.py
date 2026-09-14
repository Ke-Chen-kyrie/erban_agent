from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "data" / "storage" / "faces"
DATABASE_PATH = BASE_DIR / "data" / "SQLite" / "users.db"

FACE_ENDPOINT = "facebody.cn-shanghai.aliyuncs.com"
