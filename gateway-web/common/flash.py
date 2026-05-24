import json

from itsdangerous import BadSignature, URLSafeSerializer


FLASH_COOKIE = "gw_flash"


def _serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret_key=secret, salt="flash-message")


def encode_flash(secret: str, message: str, category: str = "success") -> str:
    return _serializer(secret).dumps({"message": message, "category": category})


def decode_flash(secret: str, cookie_value: str | None) -> dict | None:
    if not cookie_value:
        return None
    try:
        data = _serializer(secret).loads(cookie_value)
    except (BadSignature, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("message"):
        return None
    return {"message": str(data["message"]), "category": str(data.get("category") or "success")}


def set_flash(response, secret: str, message: str, category: str = "success"):
    response.set_cookie(
        FLASH_COOKIE,
        encode_flash(secret, message, category),
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=30,
        path="/",
    )
    return response


def pop_flash(request, response):
    flash = decode_flash(request.app.state.session_secret, request.cookies.get(FLASH_COOKIE))
    if flash:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return flash
