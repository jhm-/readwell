from readwell.web.tidy import tidy_html


def test_spacers_ads_and_lazy_images():
    html = ('<p class="h">Headline</p><img src="https://cdn.test/template/images/spacer_large.gif" />'
            '<img alt="Oil sands" src="https://cdn.test/assets/suncor.jpeg" />'
            '<p>Text.</p><p>Advertisement.<br /><img src="https://radio.test/uploads/Lake-Awry.png?ssl=1" /></p>'
            '<img src="https://t.test/open.gif" width="1" height="1">'
            '<img class="wp-image lazy" data-src="https://site.test/photo.jpg" src="data:image/gif;base64,R0lGOD">'
            '<img class="ad-slot" src="https://ads.test/banner.jpg">'
            '<img alt="Ad hoc committee meets" src="https://site.test/committee.jpg">')
    out = tidy_html(html)
    assert "spacer_large" not in out and "Lake-Awry" not in out and "open.gif" not in out
    assert "banner.jpg" not in out and "data:image" not in out
    assert out.index("suncor.jpeg") < out.index("photo.jpg")  # the real photo is now first
    assert "committee.jpg" in out and "<p>Text.</p>" in out   # "Ad hoc" in alt text is not an ad


def test_untouched_html_is_returned_as_is():
    html = '<p>No pictures here.</p>'
    assert tidy_html(html) == html
    assert tidy_html(None) == ""
