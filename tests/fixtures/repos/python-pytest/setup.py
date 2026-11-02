from pathlib import Path

Path(__file__).with_name("SETUP_CANARY_EXECUTED").write_text("imported", encoding="utf-8")
