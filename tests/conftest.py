import pytest

from phototripper import common
from phototripper.common import Context


@pytest.fixture(autouse=True)
def reset_context():
    """``Context.set`` refuses a second call, so clear the singleton per test."""
    Context.context = None
    yield
    Context.context = None


@pytest.fixture
def ini(tmp_path, monkeypatch):
    """Write a phototripper.ini and make the config lookup find it."""

    def _write(text):
        path = tmp_path / common.APP_CONFIG_FILENAME
        path.write_text(text)
        monkeypatch.chdir(tmp_path)
        return path

    return _write
