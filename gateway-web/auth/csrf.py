import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


CSRF_COOKIE = "gw_csrf"
CSRF_MAX_AGE = 3600


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret, salt="csrf-token")


def create_csrf_token(secret: str) -> str:
    return _serializer(secret).dumps(secrets.token_urlsafe(32))


def verify_csrf_token(secret: str, token: str | None, cookie_token: str | None) -> bool:
    if not token or not cookie_token or token != cookie_token:
        return False
    try:
        _serializer(secret).loads(token, max_age=CSRF_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False
