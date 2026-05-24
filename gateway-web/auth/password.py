import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def validate_password_policy(password: str) -> tuple[bool, str | None]:
    if len(password) < 12:
        return False, "비밀번호는 최소 12자 이상이어야 합니다."
    classes = [
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ]
    if sum(classes) < 3:
        return False, "비밀번호는 영문 대소문자, 숫자, 특수문자 중 3종 이상을 포함해야 합니다."
    return True, None
