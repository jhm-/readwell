from readwell import cli
from readwell.fetch.http import Fetcher

from .conftest import write_sources

PAGE = "<html><body>" + "".join(
    f'<div class="card"><h3><a href="/a/{n}">Asset number {n}</a></h3></div>' for n in range(3)) + "</body></html>"


def test_check_handles_every_kind_and_survives_a_crash(site, dirs, monkeypatch, capsys):
    config_dir, data_dir = dirs
    monkeypatch.setattr(cli, "Fetcher", lambda c: Fetcher(c, transport=site.transport))
    site.add("https://list.test/", PAGE)
    write_sources(config_dir, "s.yaml", [
        {"id": "assets", "kind": "listing", "url": "https://list.test/", "item": "div.card",
         "fields": {"title": {"sel": "h3"}, "link": {"sel": "a", "attr": "href"}}},
        {"id": "oic", "kind": "numbered", "url_template": "https://x.test/{year}/{n}", "start": {"year": 2026, "n": 1}},
        {"id": "inbox", "kind": "email", "imap": {"host": "imap.test", "user": "u", "password_env": "NOPE_PW"}},
    ])
    monkeypatch.delenv("NOPE_PW", raising=False)
    rc = cli.main(["--config-dir", str(config_dir), "--data-dir", str(data_dir), "check"])
    out = capsys.readouterr().out
    assert "ok    assets" in out and "3 items" in out
    assert "skip  oic" in out
    assert "FAIL  inbox" in out and "NOPE_PW" in out
    assert rc == 1
