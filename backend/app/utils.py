from datetime import datetime, timezone


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for timezone-aware columns; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
