"""Shared test fixtures with mock ConnectionManager."""

import pytest


class MockConnectionManager:
    """Fake ConnectionManager that returns canned data."""

    def __init__(self, responses=None):
        self._responses = responses or {}
        self._calls = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def fetch_all(self, query, params=()):
        self._calls.append(("fetch_all", query, params))
        for key, value in self._responses.items():
            if key in query:
                return value
        return []

    async def fetch_one(self, query, params=()):
        self._calls.append(("fetch_one", query, params))
        for key, value in self._responses.items():
            if key in query:
                return value
        return None

    async def has_extension(self, name):
        return self._responses.get(f"ext:{name}", False)

    async def get_server_version(self):
        return "PostgreSQL 15.4"


@pytest.fixture
def mock_db():
    return MockConnectionManager()
