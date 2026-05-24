from auth.password import hash_password, validate_password_policy, verify_password


def test_password_hash_roundtrip():
    hashed = hash_password("LongEnough123!")
    assert verify_password("LongEnough123!", hashed)
    assert not verify_password("wrong-password", hashed)


def test_password_policy():
    ok, _ = validate_password_policy("LongEnough123!")
    assert ok
    ok, _ = validate_password_policy("short")
    assert not ok
