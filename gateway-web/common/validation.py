import re
from datetime import datetime
from decimal import Decimal, InvalidOperation


TEAM_KEY_RE = re.compile(r"^[a-z0-9-]+$")


def validate_team_key(value: str) -> tuple[bool, str | None]:
    if not value:
        return False, "team_key는 필수입니다."
    if not TEAM_KEY_RE.fullmatch(value):
        return False, "team_key는 소문자, 숫자, 하이픈만 사용할 수 있습니다."
    return True, None


def parse_optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_money(value: str | None, field_name: str) -> tuple[Decimal | None, str | None]:
    try:
        money = Decimal(str(value or "0"))
    except InvalidOperation:
        return None, f"{field_name}는 숫자여야 합니다."
    if money < 0:
        return None, f"{field_name}는 0 이상이어야 합니다."
    return money, None


def parse_alert_threshold(value: str | None) -> tuple[int | None, str | None]:
    try:
        threshold = int(value or "80")
    except ValueError:
        return None, "알림 임계값은 1부터 100 사이의 정수여야 합니다."
    if threshold < 1 or threshold > 100:
        return None, "알림 임계값은 1부터 100 사이의 정수여야 합니다."
    return threshold, None
