import pytest

from orchid_demo.engine import Engine
from test_operator_engine import Clock


@pytest.fixture
def engine(tmp_path):
    """A simulated engine on a fake clock. A test file that needs it connected or calibrated overrides this."""
    clock = Clock()
    e = Engine(tmp_path, clock=clock, sleep=clock.sleep)
    yield e
    e.close()
