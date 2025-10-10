import musicnames.patterns as patterns


def test_dash_split_supports_common_dashes():
    value = "Artist – Title — Remix"
    parts = [p for p in patterns.DASH_SPLIT_RE.split(value) if p.strip()]
    assert parts == ["Artist", "Title", "Remix"]


def test_featured_token_detection():
    assert patterns.FEATURED_TOKEN_RE.search("Artist feat. Guest")
    assert patterns.FEATURED_TOKEN_RE.search("Artist x Guest")


def test_multi_artist_separator():
    assert patterns.MULTI_ARTIST_SEPARATOR_RE.search("Artist & Guest")
    assert patterns.MULTI_ARTIST_SEPARATOR_RE.search("Artist, Guest")


def test_track_number_prefix():
    match = patterns.TRACK_NUMBER_RE.match("01 - Song Name")
    assert match
    assert match.group("track") == "01"
    assert match.group("rest") == "Song Name"


def test_bracket_tag_detection():
    assert patterns.BRACKET_TAG_RE.search("Song (Official Video)")
    assert not patterns.BRACKET_TAG_RE.search("Song (Piano)")
