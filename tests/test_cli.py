import pytest

import desk.cli
from desk.cli import main


def test_help_exits_cleanly(monkeypatch):
    monkeypatch.setattr(desk.cli, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
