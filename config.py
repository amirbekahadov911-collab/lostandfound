"""All settings are read from environment variables (or a .env file)."""
import os
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
REMINDER_TIME = time.fromisoformat(os.getenv("REMINDER_TIME", "08:00")).replace(tzinfo=TIMEZONE)

# python-telegram-bot numbers days 0=Sunday ... 6=Saturday
_DAY_NUMBERS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
REMINDER_DAYS = tuple(
    _DAY_NUMBERS[d.strip().lower()[:3]]
    for d in os.getenv("REMINDER_DAYS", "mon,tue,wed,thu,fri").split(",")
    if d.strip()
)

# Suggested claim points shown as buttons (people can still type their own)
CLAIM_LOCATIONS = [
    s.strip() for s in os.getenv("CLAIM_LOCATIONS", "School office|Reception|Security desk").split("|") if s.strip()
]

# Items drop out of the daily loop after this many days (0 = never)
ITEM_EXPIRY_DAYS = int(os.getenv("ITEM_EXPIRY_DAYS", "30"))
MAX_ACTIVE_PER_USER = int(os.getenv("MAX_ACTIVE_PER_USER", "10"))

# Seconds between reposts during the daily reminder (keeps us under Telegram's group rate limit)
REPOST_DELAY = float(os.getenv("REPOST_DELAY", "3"))

# Auto-moderation
FLOOD_LIMIT = int(os.getenv("FLOOD_LIMIT", "6"))  # messages ...
FLOOD_WINDOW = int(os.getenv("FLOOD_WINDOW", "10"))  # ... within this many seconds
BLOCK_LINKS = os.getenv("BLOCK_LINKS", "true").lower() in ("1", "true", "yes")

DB_PATH = os.getenv("DB_PATH", "lostfound.db")
