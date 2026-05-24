import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("collector_main", MODULE_PATH)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_dlp_blocks_new_krn():
    result = collector.dlp_check_strict("주민번호 900101-1234567")
    assert result == {"blocked": True, "pattern": "krn"}


def test_dlp_redacts_history_card_number():
    result = collector.dlp_redact("card 4111-1111-1111-1111")
    assert result["redacted_text"] == "card [REDACTED-CARD]"
    assert result["applied"] == ["card"]


def test_latest_user_message_detection():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "latest"},
    ]
    assert collector.find_last_user_message_index(messages) == 2
