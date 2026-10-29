import pytest

from axiom_forex import build_fixture_platform
from fixtures.m1 import make_environment


@pytest.fixture
def env():
    """Fresh, isolated M1 environment for each test."""
    return make_environment()


@pytest.fixture
def platform():
    """Fresh repository-local fixture platform with in-memory persistence."""
    instance = build_fixture_platform()
    try:
        yield instance
    finally:
        instance.close()
