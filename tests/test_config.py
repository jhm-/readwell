import shutil

import pytest

from readwell.config import ConfigError, fingerprint, load_config

from .conftest import write_sources


def test_list_file_with_defaults_and_single_file(dirs):
    config_dir, data_dir = dirs
    write_sources(config_dir, "starter.yaml", {
        "defaults": {"kind": "feed", "interval_min": 30, "tags": ["news"]},
        "sources": [
            {"id": "one", "url": "https://one.example/"},
            {"id": "two", "url": "https://two.example/", "interval_min": 120, "note": "extra keys are fine"},
        ],
    })
    write_sources(config_dir, "single.yaml", {"id": "three", "feed_url": "https://three.example/rss"})
    cfg = load_config(config_dir, data_dir)
    assert set(cfg.sources) == {"one", "two", "three"}
    assert cfg.sources["one"].interval_min == 30 and cfg.sources["one"].tags == ["news"]
    assert cfg.sources["two"].interval_min == 120
    assert cfg.sources["three"].interval_min == 60  # settings.worker.default_interval_min
    assert cfg.errors == []


def test_bad_entries_are_reported_not_fatal(dirs):
    config_dir, data_dir = dirs
    write_sources(config_dir, "a.yaml", {"sources": [
        {"id": "ok", "url": "https://ok.example/"},
        {"id": "Bad Id", "url": "https://x/"},
        {"id": "nourl"},
        {"id": "ok", "url": "https://dupe.example/"},
        {"id": "weird", "kind": "carrier-pigeon", "url": "https://x/"},
    ]})
    (config_dir / "sources" / "broken.yaml").write_text("id: [unclosed\n")
    cfg = load_config(config_dir, data_dir)
    assert list(cfg.sources) == ["ok"]
    assert len(cfg.errors) == 5


def test_invalid_settings_are_fatal(dirs):
    config_dir, data_dir = dirs
    (config_dir / "config.yaml").write_text("server:\n  prot: 1\n")
    with pytest.raises(ConfigError):
        load_config(config_dir, data_dir)


def test_cache_used_when_share_missing(dirs):
    config_dir, data_dir = dirs
    write_sources(config_dir, "a.yaml", [{"id": "one", "url": "https://one.example/"}])
    load_config(config_dir, data_dir)
    shutil.rmtree(config_dir)
    cfg = load_config(config_dir, data_dir)
    assert cfg.from_cache and "one" in cfg.sources


def test_no_config_and_no_cache_is_fatal(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope", tmp_path / "data")


def test_fingerprint_changes_on_edit(dirs):
    config_dir, _ = dirs
    before = fingerprint(config_dir)
    write_sources(config_dir, "new.yaml", [{"id": "n", "url": "https://n.example/"}])
    assert fingerprint(config_dir) != before
