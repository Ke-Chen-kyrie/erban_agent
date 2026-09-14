import os

FOXGLOVE_BRIDGE_URL = os.getenv("FOXGLOVE_BRIDGE_URL", "ws://localhost:8765")
IDENT_API_BASE = os.getenv("IDENT_API_BASE", "http://localhost:8001")
FACE_RECOGNITION_THRESHOLD = float(os.getenv("FACE_RECOGNITION_THRESHOLD", "0.5"))