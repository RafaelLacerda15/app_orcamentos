import os
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_SERVER_TIMEZONE = "America/Manaus"


@lru_cache(maxsize=1)
def _get_zoneinfo() -> ZoneInfo | None:
    tz_name = os.getenv("APP_TIMEZONE", DEFAULT_SERVER_TIMEZONE).strip() or DEFAULT_SERVER_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        if tz_name != DEFAULT_SERVER_TIMEZONE:
            try:
                return ZoneInfo(DEFAULT_SERVER_TIMEZONE)
            except ZoneInfoNotFoundError:
                return None
        return None


def server_now() -> datetime:
    # Persistimos datetime sem timezone no banco, mas calculado no fuso configurado do servidor.
    zoneinfo = _get_zoneinfo()
    if zoneinfo is None:
        # Fallback para horario local do sistema quando tzdata nao estiver disponivel.
        return datetime.now()
    return datetime.now(zoneinfo).replace(tzinfo=None)
