"""Test envconfig — critical path functions.

Focused unit tests for the config document service:
_normalize_servers, mask_document, parse_document, upsert_static_server,
_encrypt_server_passwords, _resolve_server_password_sentinels.

These complement the existing integration tests in test_envconfig.py which
exercise the API endpoints.
"""

from __future__ import annotations

import pytest

from app.services.envconfig import (
    ConfigValidationError,
    SECRET_MASK,
    _normalize_servers,
    _encrypt_server_passwords,
    _encrypt_document_secrets,
    _resolve_secret_sentinels,
    _resolve_server_password_sentinels,
    mask_document,
    parse_document,
    upsert_static_server,
)
from app.services.crypto import FERNET_PREFIX, encrypt_secret, decrypt_secret


class TestNormalizeServers:
    """_normalize_servers — hostname keying, legacy system_id, role validation."""

    def test_basic_hostnames(self):
        servers = {
            "node01": {
                "ip": "10.0.0.1",
                "ssh_user": "root",
                "roles": ["k8s_control_plane"],
            },
        }
        result = _normalize_servers(servers, [])
        assert "node01" in result
        assert result["node01"]["source"] == "static"
        assert result["node01"]["ssh_auth_method"] is None

    def test_with_auth_method(self):
        servers = {
            "node01": {
                "ssh_auth_method": "password",
                "ssh_password": "secret",
                "roles": ["compute"],
            },
        }
        result = _normalize_servers(servers, [])
        assert result["node01"]["ssh_auth_method"] == "password"
        assert result["node01"]["ssh_password"] == "secret"

    def test_legacy_system_id_keying(self):
        servers = {
            "abc123": {"hostname": "node01", "roles": ["compute"]},
        }
        result = _normalize_servers(servers, [])
        assert "node01" in result
        assert result["node01"]["system_id"] == "abc123"
        assert result["node01"]["source"] == "maas"

    def test_source_inferred_from_system_id(self):
        servers = {
            "node01": {"system_id": "abc123", "roles": ["compute"]},
        }
        result = _normalize_servers(servers, [])
        assert result["node01"]["source"] == "maas"

    def test_source_inferred_static_without_system_id(self):
        servers = {
            "node01": {"roles": ["compute"]},
        }
        result = _normalize_servers(servers, [])
        assert result["node01"]["source"] == "static"

    def test_explicit_static_source(self):
        servers = {
            "node01": {"source": "static", "roles": ["compute"]},
        }
        result = _normalize_servers(servers, [])
        assert result["node01"]["source"] == "static"

    def test_invalid_source_raises(self):
        servers = {
            "node01": {"source": "invalid", "roles": ["compute"]},
        }
        with pytest.raises(ConfigValidationError, match="source must be one of"):
            _normalize_servers(servers, [])

    def test_unknown_role_raises(self):
        servers = {
            "node01": {"roles": ["invalid_role"]},
        }
        with pytest.raises(ConfigValidationError, match="unknown role 'invalid_role'"):
            _normalize_servers(servers, [])

    def test_invalid_hostname_raises(self):
        servers = {
            "bad hostname!": {"roles": ["compute"]},
        }
        with pytest.raises(ConfigValidationError, match="invalid hostname"):
            _normalize_servers(servers, [])

    def test_non_mapping_entry_raises(self):
        servers = {
            "node01": "not_a_mapping",
        }
        with pytest.raises(ConfigValidationError, match="must be a mapping"):
            _normalize_servers(servers, [])

    def test_duplicate_hostname_warns(self):
        warnings: list[str] = []
        servers = {
            "sys01": {"hostname": "node01", "roles": ["compute"]},
            "sys02": {"hostname": "node01", "roles": ["control"]},
        }
        result = _normalize_servers(servers, warnings)
        assert "node01" in result
        assert len(warnings) == 1
        assert "duplicate hostname" in warnings[0]

    def test_valid_roles_pass(self):
        for role in (
            "k8s_control_plane",
            "etcd",
            "control",
            "compute",
            "network",
            "storage",
            "storage-ceph",
            "storage-cinder",
        ):
            servers = {
                "node01": {"roles": [role]},
            }
            result = _normalize_servers(servers, [])
            assert result["node01"]["source"] == "static"

    def test_role_case_insensitive(self):
        servers = {
            "node01": {"roles": ["COMPUTE"]},
        }
        result = _normalize_servers(servers, [])
        assert "node01" in result


class TestMaskDocument:
    """mask_document — secrets and server ssh_password. A maas block is dropped."""

    def test_nothing_to_mask_returns_same_doc(self):
        doc = {"provider": "kubespray"}
        result = mask_document(doc)
        assert result is doc

    def test_masks_secret_data(self):
        doc = {
            "secrets": {
                "db-creds": {
                    "namespace": "openstack",
                    "data": {"password": "supersecret", "token": "abc123"},
                },
            },
        }
        result = mask_document(doc)
        assert result is not doc
        assert result["secrets"]["db-creds"]["data"]["password"] == SECRET_MASK
        assert result["secrets"]["db-creds"]["data"]["token"] == SECRET_MASK

    def test_drops_maas_block(self):
        doc = {
            "maas": {"url": "http://maas.local", "api_key": "ck:real:token"},
            "provider": "kubespray",
        }
        result = mask_document(doc)
        assert "maas" not in result
        assert result["provider"] == "kubespray"
        assert "maas" in doc

    def test_masks_server_ssh_passwords(self):
        doc = {
            "servers": {
                "node01": {
                    "ip": "10.0.0.1",
                    "ssh_password": "plainpass",
                    "roles": ["compute"],
                },
            },
        }
        result = mask_document(doc)
        assert result["servers"]["node01"]["ssh_password"] == SECRET_MASK

    def test_mask_returns_new_dict(self):
        doc = {"secrets": {"s": {"data": {"k": "v"}}}}
        result = mask_document(doc)
        assert result is not doc
        assert result["secrets"] is not doc["secrets"]

    def test_empty_secrets_returns_copy(self):
        doc = {"secrets": {}}
        result = mask_document(doc)
        assert result is not doc
        assert result["secrets"] == {}

    def test_drops_empty_maas_block(self):
        doc = {"maas": {"url": "http://maas.local", "api_key": ""}}
        result = mask_document(doc)
        assert "maas" not in result
        assert result is not doc

    def test_server_no_password_no_mask(self):
        doc = {"servers": {"node01": {"ip": "10.0.0.1", "roles": ["compute"]}}}
        assert mask_document(doc) is doc


class TestParseDocument:
    """parse_document — YAML parsing, validation, warnings."""

    def test_valid_yaml(self):
        doc, warnings = parse_document("provider: kubespray")
        assert doc == {"provider": "kubespray"}
        assert not warnings

    def test_empty_string(self):
        doc, warnings = parse_document("")
        assert doc == {}

    def test_invalid_yaml_raises(self):
        with pytest.raises(ConfigValidationError, match="YAML parse error"):
            parse_document("key: [invalid")

    def test_non_mapping_top_level_raises(self):
        with pytest.raises(ConfigValidationError, match="must be a YAML mapping"):
            parse_document("- just a list")

    def test_unknown_top_level_key_warns(self):
        doc, warnings = parse_document("provider: kubespray\nbogus_key: 123")
        assert "bogus_key" in doc
        assert len(warnings) == 1
        assert "Unknown top-level key 'bogus_key'" in warnings[0]

    def test_known_top_level_keys_no_warning(self):
        doc, warnings = parse_document(
            "provider: kubespray\nservers: {}\nmaas: {}\nsecrets: {}\n"
            "network: {}\nstorage: {}\ncomponents: {}\nchart_versions: {}\n"
            "helm_overrides: {}\nkustomize_patches: {}\ngroup_vars: {}\n"
            "talos: {}\npxe:\n  interface: eth0\n  range_start: 10.0.0.1\n  range_end: 10.0.0.2\n"
            "deploy: {}\n"
        )
        assert not warnings

    def test_servers_must_be_mapping(self):
        with pytest.raises(ConfigValidationError, match="'servers' must be a mapping"):
            parse_document("servers: [node1]")

    def test_group_vars_must_be_mapping(self):
        with pytest.raises(
            ConfigValidationError, match="'group_vars' must be a mapping"
        ):
            parse_document("group_vars: [all]")

    def test_group_vars_invalid_name(self):
        with pytest.raises(ConfigValidationError, match="invalid group name"):
            parse_document("group_vars:\n  INVALID: {}\n")

    def test_group_vars_value_must_be_mapping(self):
        with pytest.raises(ConfigValidationError, match="must be a mapping of var"):
            parse_document("group_vars:\n  all: not_a_map\n")

    def test_network_unknown_key_warns(self):
        _, warnings = parse_document("network:\n  gateway_domain: x\n  bogus_key: y\n")
        assert any("unknown key 'bogus_key'" in w for w in warnings)

    def test_storage_unknown_key_warns(self):
        _, warnings = parse_document(
            "storage:\n  cinder_backend_name: x\n  bogus_key: y\n"
        )
        assert any("unknown key 'bogus_key'" in w for w in warnings)

    def test_storage_must_be_mapping(self):
        with pytest.raises(ConfigValidationError, match="'storage' must be a mapping"):
            parse_document("storage: not_a_map\n")

    def test_secret_name_invalid(self):
        with pytest.raises(ConfigValidationError, match="invalid secret name"):
            parse_document("secrets:\n  INVALID: {data: {k: v}}\n")

    def test_secret_data_must_be_mapping(self):
        with pytest.raises(ConfigValidationError, match="data must be a mapping"):
            parse_document("secrets:\n  valid: {data: [1,2]}\n")

    def test_secret_data_value_must_be_string(self):
        with pytest.raises(ConfigValidationError, match="must be a string"):
            parse_document("secrets:\n  valid: {data: {key: 123}}\n")

    def test_pxe_missing_required_keys(self):
        with pytest.raises(ConfigValidationError, match="missing required key"):
            parse_document("pxe:\n  interface: eth0\n")

    def test_pxe_http_port_must_be_int(self):
        with pytest.raises(ConfigValidationError, match="http_port must be an integer"):
            parse_document(
                "pxe:\n  interface: eth0\n  range_start: 10.0.0.1\n  range_end: 10.0.0.2\n  http_port: '8080'\n"
            )


class TestUpsertStaticServer:
    """upsert_static_server — create/update server entries."""

    def _make_db_and_env(self):
        from app.db import create_db_engine, Base
        from app import models  # noqa: F401 — populate metadata
        from app.models import Environment
        from sqlalchemy.orm import sessionmaker

        engine = create_db_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        env = Environment(
            name=f"test-upsert-{__import__('uuid').uuid4().hex[:8]}",
            region="lab",
            tier="dev",
        )
        db.add(env)
        db.commit()
        return db, env

    def test_create_static_server(self):
        db, env = self._make_db_and_env()
        try:
            row, warnings = upsert_static_server(
                db,
                env,
                "test-actor",
                hostname="static-01",
                ip="10.0.0.50",
                ssh_user="root",
                roles=["compute"],
            )
            db.commit()
            assert row.version == 1
            assert not warnings
            row2 = db.get(type(row), row.id)
            assert "static-01" in row2.yaml_text
        finally:
            db.close()

    def test_persists_ssh_auth_method(self):
        db, env = self._make_db_and_env()
        try:
            upsert_static_server(
                db,
                env,
                "test-actor",
                hostname="static-01",
                ip="10.0.0.50",
                ssh_auth_method="password",
                ssh_password="plainpass",
                roles=["compute"],
            )
            db.commit()
            from app.services.envconfig import get_current

            current = get_current(db, env)
            assert current is not None
            servers = current[0]["servers"]
            assert servers["static-01"]["ssh_auth_method"] == "password"
            pwd = servers["static-01"]["ssh_password"]
            assert pwd.startswith(FERNET_PREFIX)
            assert decrypt_secret(pwd) == "plainpass"
        finally:
            db.close()

    def test_persists_ssh_password_encrypted(self):
        db, env = self._make_db_and_env()
        try:
            row, _ = upsert_static_server(
                db,
                env,
                "test-actor",
                hostname="node01",
                ssh_auth_method="password",
                ssh_password="mysecret",
                roles=["control"],
            )
            db.commit()
            from app.services.envconfig import get_current

            current = get_current(db, env)
            pwd = current[0]["servers"]["node01"]["ssh_password"]
            assert pwd.startswith(FERNET_PREFIX)
            assert "mysecret" not in pwd
            assert decrypt_secret(pwd) == "mysecret"
        finally:
            db.close()

    def test_update_existing_server_increments_version(self):
        db, env = self._make_db_and_env()
        try:
            row1, _ = upsert_static_server(
                db,
                env,
                "test-actor",
                hostname="node01",
                roles=["compute"],
            )
            db.commit()
            assert row1.version == 1
            row2, _ = upsert_static_server(
                db,
                env,
                "test-actor",
                hostname="node01",
                roles=["compute", "control"],
            )
            db.commit()
            assert row2.version == 2
            from app.services.envconfig import get_current

            current = get_current(db, env)
            assert set(current[0]["servers"]["node01"]["roles"]) == {
                "compute",
                "control",
            }
        finally:
            db.close()

    def test_invalid_hostname_raises(self):
        db, env = self._make_db_and_env()
        try:
            with pytest.raises(ConfigValidationError, match="Invalid hostname"):
                upsert_static_server(
                    db,
                    env,
                    "actor",
                    hostname="bad host!",
                )
        finally:
            db.close()


class TestEncryptServerPasswords:
    """_encrypt_server_passwords — encrypt in place, idempotent."""

    def test_encrypts_plaintext_password(self):
        doc = {"servers": {"node01": {"ssh_password": "mypass"}}}
        _encrypt_server_passwords(doc)
        pwd = doc["servers"]["node01"]["ssh_password"]
        assert pwd.startswith(FERNET_PREFIX)
        assert decrypt_secret(pwd) == "mypass"

    def test_skips_already_encrypted(self):
        encrypted = encrypt_secret("mypass")
        doc = {"servers": {"node01": {"ssh_password": encrypted}}}
        _encrypt_server_passwords(doc)
        assert doc["servers"]["node01"]["ssh_password"] == encrypted

    def test_skips_secret_mask(self):
        doc = {"servers": {"node01": {"ssh_password": SECRET_MASK}}}
        _encrypt_server_passwords(doc)
        assert doc["servers"]["node01"]["ssh_password"] == SECRET_MASK

    def test_skips_empty_password(self):
        doc = {"servers": {"node01": {"ssh_password": ""}}}
        _encrypt_server_passwords(doc)
        assert doc["servers"]["node01"]["ssh_password"] == ""

    def test_returns_true_when_not_mask(self):
        """_encrypt_server_passwords marks changed=True for any non-mask password
        (encrypt_secret is idempotent, so the value doesn't change, but the
        flag is set — callers use it to know a re-dump may be needed)."""
        doc = {"servers": {"node01": {"ssh_password": encrypt_secret("x")}}}
        assert _encrypt_server_passwords(doc) is True

    def test_returns_true_when_changed(self):
        doc = {"servers": {"node01": {"ssh_password": "mypass"}}}
        assert _encrypt_server_passwords(doc) is True

    def test_no_servers_section(self):
        doc = {"provider": "kubespray"}
        assert _encrypt_server_passwords(doc) is False


class TestResolveServerPasswordSentinels:
    """_resolve_server_password_sentinels — replace SECRET_MASK with previous."""

    def test_replaces_sentinel_with_previous(self):
        previous_pwd = encrypt_secret("oldpass")
        previous = {"servers": {"node01": {"ssh_password": previous_pwd}}}
        doc = {"servers": {"node01": {"ssh_password": SECRET_MASK}}}
        _resolve_server_password_sentinels(doc, previous)
        assert doc["servers"]["node01"]["ssh_password"] == previous_pwd

    def test_keeps_sentinel_when_no_previous(self):
        doc = {"servers": {"node01": {"ssh_password": SECRET_MASK}}}
        _resolve_server_password_sentinels(doc, None)
        assert doc["servers"]["node01"]["ssh_password"] == SECRET_MASK

    def test_keeps_sentinel_when_no_previous_server(self):
        previous = {"servers": {}}
        doc = {"servers": {"node01": {"ssh_password": SECRET_MASK}}}
        _resolve_server_password_sentinels(doc, previous)
        assert doc["servers"]["node01"]["ssh_password"] == SECRET_MASK

    def test_non_sentinel_password_unchanged(self):
        doc = {"servers": {"node01": {"ssh_password": "newpass"}}}
        _resolve_server_password_sentinels(doc, None)
        assert doc["servers"]["node01"]["ssh_password"] == "newpass"


class TestEncryptDocumentSecrets:
    """_encrypt_document_secrets — encrypt secrets.*.data in place."""

    def test_encrypts_secret_data(self):
        doc = {"secrets": {"db": {"data": {"password": "secret123"}}}}
        _encrypt_document_secrets(doc)
        val = doc["secrets"]["db"]["data"]["password"]
        assert val.startswith(FERNET_PREFIX)
        assert decrypt_secret(val) == "secret123"

    def test_idempotent_on_encrypted(self):
        encrypted = encrypt_secret("x")
        doc = {"secrets": {"s": {"data": {"k": encrypted}}}}
        _encrypt_document_secrets(doc)
        assert doc["secrets"]["s"]["data"]["k"] == encrypted

    def test_no_secrets_section(self):
        doc = {"provider": "kubespray"}
        _encrypt_document_secrets(doc)


class TestResolveSecretSentinels:
    """_resolve_secret_sentinels — replace SECRET_MASK in secrets.*.data."""

    def test_replaces_sentinel(self):
        previous = {"secrets": {"db": {"data": {"password": "encrypted_val"}}}}
        doc = {"secrets": {"db": {"data": {"password": SECRET_MASK}}}}
        _resolve_secret_sentinels(doc, previous)
        assert doc["secrets"]["db"]["data"]["password"] == "encrypted_val"

    def test_keeps_sentinel_no_previous(self):
        doc = {"secrets": {"db": {"data": {"password": SECRET_MASK}}}}
        _resolve_secret_sentinels(doc, None)
        assert doc["secrets"]["db"]["data"]["password"] == SECRET_MASK

    def test_non_sentinel_unchanged(self):
        doc = {"secrets": {"db": {"data": {"password": "newval"}}}}
        _resolve_secret_sentinels(doc, None)
        assert doc["secrets"]["db"]["data"]["password"] == "newval"
