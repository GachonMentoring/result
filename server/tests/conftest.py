import pytest


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch):
    """개발자 PC 에 DB_PASSWORD 가 설정돼 있어도 테스트가 실제 DB 에 붙지 않게."""
    monkeypatch.delenv("DB_PASSWORD", raising=False)
