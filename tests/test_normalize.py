from readwell.models import RawItem
from readwell.pipeline.normalize import canonical_url, clean_html, normalize
from readwell.pipeline.titles import similar, title_key, title_tokens


def test_canonical_strips_tracking_and_cosmetics():
    a = canonical_url("https://www.Example.com/news/story/?utm_source=rss&utm_medium=x&id=7&fbclid=zz#top")
    b = canonical_url("http://example.com:80/news/story?id=7")
    assert a == b == "https://example.com/news/story?id=7"


def test_canonical_amp_variants_collapse():
    assert canonical_url("https://amp.example.com/a/b/amp/") == canonical_url("https://example.com/a/b")


def test_canonical_keeps_meaningful_params_sorted():
    assert canonical_url("https://x.com/p?b=2&a=1") == "https://x.com/p?a=1&b=2"


def test_canonical_non_http_untouched():
    assert canonical_url("mailto:me@example.com") == "mailto:me@example.com"
    assert canonical_url(None) is None


def test_clean_html_removes_active_content():
    html = '<p onclick="evil()">Hi</p><script>bad()</script><iframe src="x"></iframe><a href="javascript:1">x</a>'
    out = clean_html(html)
    assert "script" not in out and "iframe" not in out and "onclick" not in out and "javascript" not in out
    assert "<p>Hi</p>" in out


def test_normalize_uses_summary_as_body_when_no_content():
    n = normalize(RawItem(guid="g1", url="https://e.com/a?utm_source=x", title="  Big   News &amp; More ",
                          summary_html="<p>one two three</p>"))
    assert n.title == "Big News & More"
    assert n.content_html == "<p>one two three</p>"
    assert n.summary is None
    assert n.word_count == 3
    assert n.canonical_url == "https://e.com/a"


def test_normalize_guid_falls_back_to_url_then_hash():
    assert normalize(RawItem(guid=None, url="https://e.com/x", title="t")).guid == "https://e.com/x"
    assert normalize(RawItem(guid=None, url=None, title="t")).guid.startswith("h:")


def test_headline_matching():
    def sim(a, b):
        return similar(title_tokens(a), title_tokens(b))

    assert sim("Territory withdraws land from staking near Ross River",
               "Territory withdraws land from staking near Ross River, First Nation says")
    assert sim("Port of Prince Rupert expands terminal capacity", "Prince Rupert port expands terminal capacity")
    assert sim("BC government pauses mineral tenure in northwest", "B.C. government pauses mineral tenure in northwest")
    assert not sim("Teck reports third quarter results", "Teck reports second quarter results")
    assert not sim("Weather", "Weather")  # too short to judge
    assert not sim("Calgary council approves new transit budget", "Kamloops council approves new parks budget")


def test_title_key_is_order_independent():
    assert title_key("Rupert port expands") == title_key("expands port Rupert") == "expands port rupert"
