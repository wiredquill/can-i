"""Tests for checks.yaml loading, validation, and persistence."""
import pytest

from cani.config import Config, ConfigError, load_config, save_config

VALID_YAML = """\
version: 1
settings:
  interval_seconds: 3
  default_timeout_seconds: 5
checks:
  - id: net-http-external
    name: Outbound HTTPS
    category: network
    command: curl -fsS --max-time 2 -o /dev/null https://suse.com
  - id: fs-read-shadow
    name: Read /etc/shadow
    category: filesystem
    command: cat /etc/shadow
    timeout: 2
    description: Sensitive file read
"""


def test_load_valid_config(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text(VALID_YAML)
    cfg = load_config(p)
    assert isinstance(cfg, Config)
    assert cfg.settings.interval_seconds == 3
    assert len(cfg.checks) == 2
    assert cfg.checks[0].id == "net-http-external"
    assert cfg.checks[0].category == "network"
    # timeout falls back to the default when omitted
    assert cfg.checks[0].timeout == 5
    # explicit timeout wins
    assert cfg.checks[1].timeout == 2


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_syntax_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("checks: [unclosed\n  - {")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(p)


def test_missing_checks_key_raises(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("version: 1\nsettings: {}\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_category_raises_with_field_detail(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "checks:\n  - id: x\n    name: X\n    category: magic\n    command: true\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    msg = str(exc.value)
    assert "category" in msg and "magic" in msg


def test_duplicate_ids_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "checks:\n"
        "  - id: dup\n    name: A\n    category: network\n    command: 'true'\n"
        "  - id: dup\n    name: B\n    category: network\n    command: 'true'\n"
    )
    with pytest.raises(ConfigError, match="dup"):
        load_config(p)


def test_empty_command_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "checks:\n  - id: x\n    name: X\n    category: process\n    command: '  '\n"
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_save_config_validates_and_persists(tmp_path):
    p = tmp_path / "checks.yaml"
    cfg = save_config(p, VALID_YAML)
    assert len(cfg.checks) == 2
    # file on disk round-trips
    assert load_config(p).checks[1].id == "fs-read-shadow"


def test_save_config_rejects_invalid_and_does_not_write(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text(VALID_YAML)
    before = p.read_text()
    with pytest.raises(ConfigError):
        save_config(p, "checks:\n  - id: x\n    category: bogus\n")
    assert p.read_text() == before  # untouched on failure


def test_env_refs_fill_commands_and_unset_ones_drop_the_check(monkeypatch):
    from cani.config import parse_config

    text = """
version: 1
checks:
  - id: a
    name: always
    category: network
    command: curl https://suse.com
  - id: b
    name: optional
    category: network
    command: curl ${CANI_TEST_URL}
"""
    monkeypatch.delenv("CANI_TEST_URL", raising=False)
    assert [c.id for c in parse_config(text).checks] == ["a"]

    monkeypatch.setenv("CANI_TEST_URL", "https://example.org/simple/")
    cfg = parse_config(text)
    assert cfg.checks[1].command == "curl https://example.org/simple/"
