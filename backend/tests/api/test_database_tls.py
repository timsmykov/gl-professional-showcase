"""Verified TLS is explicit, fail closed and identical across both drivers."""

import ssl
from pathlib import Path

import pytest
from pydantic import ValidationError

from gl_api.config import ConfigurationError, MigrationSettings, Settings


def test_synthetic_database_can_disable_tls_explicitly():
    settings = Settings(database_tls_mode="disable")
    assert settings.database_ssl() is False
    assert settings.migration_connect_args() == {"sslmode": "disable"}


@pytest.mark.parametrize("mode", ["disable", "require", "prefer", "verify-ca"])
def test_required_database_tls_rejects_non_verified_modes(mode):
    with pytest.raises(ValidationError):
        Settings(require_database_tls=True, database_tls_mode=mode)


def test_verified_tls_requires_ca():
    with pytest.raises(ValidationError):
        Settings(database_tls_mode="verify-full")


@pytest.mark.parametrize("missing", [True, False])
def test_invalid_ca_never_falls_back_or_echoes_path(tmp_path, missing):
    path = tmp_path / "synthetic-private-path"
    if not missing:
        path.write_text("synthetic-private-invalid-certificate")
    settings = Settings(database_tls_mode="verify-full", database_ca_file=path)
    for operation in [settings.database_ssl, settings.migration_connect_args]:
        with pytest.raises(ConfigurationError) as caught:
            operation()
        assert "synthetic-private" not in str(caught.value)


def test_both_drivers_require_ca_and_hostname_validation():
    ca = Path(__file__).resolve().parents[1] / "fixtures/synthetic-unit-ca.pem"
    settings = Settings(
        database_tls_mode="verify-full", require_database_tls=True, database_ca_file=ca
    )
    context = settings.database_ssl()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert settings.migration_connect_args() == {"sslmode": "verify-full", "sslrootcert": str(ca)}


def test_migration_and_application_roles_are_independently_configurable(monkeypatch):
    monkeypatch.setenv("GL_API_MIGRATION_APP_ROLE", "gl_dev_app")
    monkeypatch.setenv("GL_API_MIGRATION_DATABASE_USER", "gl_dev_migrator")
    settings = MigrationSettings()
    assert settings.database_user == "gl_dev_migrator"
    assert settings.application_role == "gl_dev_app"
