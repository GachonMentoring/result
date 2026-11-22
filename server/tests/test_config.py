"""설정: 로컬과 파드 안에서 기본값이 맞게 갈리는지 (배포용 수정 후에도 로컬 동작 유지)."""

from pathlib import Path

import pytest

from app import config


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv("MANIFEST_DIR", raising=False)
    monkeypatch.delenv("WATCH_NAMESPACE", raising=False)


def test_local_uses_dev_manifests(no_env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SA_NAMESPACE_FILE", tmp_path / "없음")          # 파드 아님
    monkeypatch.setattr(config, "DEV_MANIFEST_DIR", tmp_path / "dev-manifests")
    assert config.resolve_manifest_dir() == tmp_path / "dev-manifests"
    assert (tmp_path / "dev-manifests").is_dir()                                # 로컬은 폴더를 만들어 줌


def test_in_cluster_defaults_to_mount_path(no_env, monkeypatch, tmp_path):
    sa = tmp_path / "namespace"
    sa.write_text("mentor")
    monkeypatch.setattr(config, "SA_NAMESPACE_FILE", sa)                        # 파드 안
    monkeypatch.setattr(config, "DEV_MANIFEST_DIR", tmp_path / "dev-manifests")
    assert config.resolve_manifest_dir() == Path("/manifests")
    assert not (tmp_path / "dev-manifests").exists()                            # 파드에서는 아무것도 만들지 않음
    assert config.resolve_namespace() == "mentor"


def test_env_overrides_everything(no_env, monkeypatch, tmp_path):
    sa = tmp_path / "namespace"
    sa.write_text("mentor")
    monkeypatch.setattr(config, "SA_NAMESPACE_FILE", sa)
    monkeypatch.setenv("MANIFEST_DIR", "/data/manifests")
    monkeypatch.setenv("WATCH_NAMESPACE", "other")
    assert config.resolve_manifest_dir() == Path("/data/manifests")
    assert config.resolve_namespace() == "other"
