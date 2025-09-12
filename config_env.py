import os

# Tokens / IDs (ajusta en Render → Environment)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
META_TOKEN = os.getenv("META_TOKEN", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
WA_API_VERSION = os.getenv("WA_API_VERSION", "v20.0")
ADVISOR_NUMBER = os.getenv("ADVISOR_NUMBER", "")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
