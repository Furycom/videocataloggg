import musicnames.normalize as normalize


def test_strip_extension_handles_audio_and_suffix():
    assert normalize.strip_extension("01-song.mp3") == "01-song"
    assert normalize.strip_extension("recording.flac.part") == "recording"
    assert normalize.strip_extension("notes.txt") == "notes.txt"


def test_swap_and_collapse_spaces():
    text = "My_Song...Final"
    swapped = normalize.swap_separators_for_spaces(text)
    assert swapped == "My Song Final"
    assert normalize.collapse_spaces("  My   Song   ") == "My Song"


def test_drop_bracketed_tags_removes_known_tokens():
    title = "Track Name (Official Video) [1080p] {Remaster}"
    assert normalize.drop_bracketed_tags(title) == "Track Name"


def test_normalise_candidate_runs_all_steps():
    raw = "01_Artist-Title (Live).mp3"
    cleaned = normalize.normalise_candidate(raw)
    assert cleaned == "01 Artist-Title"


def test_unique_non_empty_preserves_order():
    values = ["Artist", "artist", "", "Title"]
    assert normalize.unique_non_empty(values) == ["Artist", "Title"]
