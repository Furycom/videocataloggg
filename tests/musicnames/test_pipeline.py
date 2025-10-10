from musicnames.parse import parse_music_name
from musicnames.score import score_parse_result
from musicnames.review import generate_review_bundle


def test_parse_and_score_happy_path():
    result = parse_music_name(
        "Artist - Song Title (Official Video).mp3",
        parents=["Artist", "Best Album"],
    )
    assert result.artist == "Artist"
    assert result.title == "Song Title"
    assert result.album == "Best Album"
    assert "dash split" in result.reasons
    score, reasons = score_parse_result(result, parents=["Artist", "Best Album"])
    assert score > 0.7
    assert "dash split" in reasons
    assert "artist and title" in reasons


def test_scoring_penalises_suspicious_artist():
    result = parse_music_name("Unknown Artist - Mystery.mp3")
    scored, reasons = score_parse_result(result)
    assert scored < 0.6
    assert "suspicious artist" in reasons


def test_review_bundle_includes_unknown_marker():
    result = parse_music_name("Track.mp3")
    score, score_reasons = score_parse_result(result)
    review = generate_review_bundle(result, score, score_reasons, threshold=0.8)
    assert review["needs_review"] is True
    assert any("mark as unknown" in suggestion for suggestion in review["suggestions"])


def test_review_bundle_recommends_parent_artist():
    result = parse_music_name(
        "Unknown - Track.mp3",
        parents=["Artist", "Album"],
    )
    score, score_reasons = score_parse_result(result)
    review = generate_review_bundle(result, score, score_reasons, threshold=0.9)
    assert any("parent folder artist" in suggestion for suggestion in review["suggestions"])
