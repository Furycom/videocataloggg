from docpreview.run import DocPreviewSettings


def test_docpreview_settings_overrides():
    base = DocPreviewSettings.from_mapping(
        {
            "enable": True,
            "max_pages": 6,
            "sample_strategy": "smart",
            "summary_target_tokens": 150,
        }
    )
    assert base.enable is True
    assert base.normalized_strategy() == "smart"
    updated = base.with_overrides(max_pages=3, sample_strategy="first")
    assert updated.max_pages == 3
    assert updated.normalized_strategy() == "first"
    assert base.max_pages == 6
    assert base.sample_strategy == "smart"
