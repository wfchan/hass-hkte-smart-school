"""Constants for the HKTE Smart School integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "hkte_smart_school"

CONF_LOGIN_NAME: Final = "login_name"
CONF_UPDATE_INTERVAL: Final = "update_interval"

DEFAULT_UPDATE_INTERVAL_MINUTES: Final = 15
MIN_UPDATE_INTERVAL_MINUTES: Final = 5
MAX_UPDATE_INTERVAL_MINUTES: Final = 60
DEFAULT_UPDATE_INTERVAL: Final = timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES)

BASE_URL: Final = "https://cls.hkteducation.com"
API_PREFIX: Final = "/api/parent3.php/"
APP_VERSION: Final = "3.4.23"
REQUEST_TIMEOUT_SECONDS: Final = 30
MAX_PAGES: Final = 200
NOTICE_PAGE_SIZE: Final = 50
MESSAGE_PAGE_SIZE: Final = 40
SEEN_IDS_PER_KIND: Final = 20000
NOTICE_CONTENT_LIMIT: Final = 20000
NOTICE_DISPLAY_LIMIT: Final = 20
MAX_ATTACHMENT_BYTES: Final = 25 * 1024 * 1024

PLATFORMS: Final = ("sensor", "calendar", "event")
