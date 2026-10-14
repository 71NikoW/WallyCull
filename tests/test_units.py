from wallycull import classify, config, updater


def test_version_tuple():
    assert updater.version_tuple("v0.10.2") > updater.version_tuple("0.9.9")


def test_config_has_all_sections():
    c = config.load()
    for section in ("general", "files", "quality", "duplicates", "classification", "learning", "ai"):
        assert section in c
    assert c["files"]["rejected_folder"] == "_rejected"


def test_value_matrix():
    c = config.load()
    base = {"id": 1, "source": "a.jpg", "files": ["a.jpg"], "width": 6000, "height": 4000, "phash": None,
            "metrics": {"scores": {"sharpness": 1, "exposure": 1, "noise": 1, "shake": 1, "resolution": 1}}}
    good_high = dict(base, quality=0.9, ai={"aesthetic": 7.0, "wallart": 0.9, "content_value": 1.0})
    poor_low = dict(base, id=2, quality=0.2, ai={"aesthetic": 3.0, "wallart": 0.1, "content_value": -1.0})
    images = classify.decide([good_high, poor_low], c)
    assert images[0]["category"] == "highlight"
    assert images[1]["category"] == "reject"
