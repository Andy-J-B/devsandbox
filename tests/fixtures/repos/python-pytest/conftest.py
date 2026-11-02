from pathlib import Path

Path(__file__).with_name("CONFTEST_CANARY_EXECUTED").write_text("imported", encoding="utf-8")
