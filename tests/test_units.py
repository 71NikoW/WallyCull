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


def test_fast_jpeg_load_keeps_full_resolution_brightness(tmp_path):
    """The fast path decodes colors at reduced size, but brightness (sharpness) stays as before."""
    import cv2
    import numpy as np

    from wallycull import loader

    rng = np.random.default_rng(0)
    img = cv2.GaussianBlur(rng.integers(0, 255, (1500, 2400, 3), dtype=np.uint8), (0, 0), 1.5)
    path = tmp_path / "x.jpg"
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    rgb, info, gray = loader.load_image(path, set(), 500, full=True, reference=2000)
    full = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    expected = cv2.resize(cv2.cvtColor(full, cv2.COLOR_BGR2GRAY), None, fx=500 / 2400, fy=500 / 2400, interpolation=cv2.INTER_AREA)
    assert rgb.shape == (312, 500, 3) and (info["width"], info["height"]) == (2400, 1500)
    assert gray.shape == (1250, 2000)
    assert np.abs(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(int) - expected).mean() < 1.0


def test_iqa_batches_by_size_with_same_scores():
    import numpy as np
    import torch
    from PIL import Image

    from wallycull import ai

    calls = {"musiq": [], "nima": []}

    def metric(name):
        def run(batch):
            calls[name].append(batch.shape[0])
            return batch.mean(dim=(1, 2, 3)).unsqueeze(1)
        return run

    class Clip:
        def encode_image(self, batch):
            return torch.ones(batch.shape[0], 4)

    m = ai.Models.__new__(ai.Models)
    m.torch, m.device, m.clip, m.laion = torch, "cpu", Clip(), None
    m.preprocess = lambda b: torch.zeros(3, 2, 2)
    m.text_content = torch.ones(len(ai.CONTENT), 4)
    m.text_questions = torch.ones(2 * len(ai.QUESTIONS), 4)
    m.iqa = {"musiq": metric("musiq"), "nima": metric("nima")}
    images = [Image.new("RGB", size, (v, v, v)) for size, v in
              (((40, 30), 10), ((30, 40), 50), ((40, 30), 90), ((40, 30), 200))]
    results = m.batch(images)
    assert calls == {"musiq": [1, 1, 1, 1], "nima": [3, 1]}
    for (r, _), v in zip(results, (10, 50, 90, 200)):
        assert r["nima"] == r["musiq"] == round(v / 255, 3)


def test_preload_hands_over_models_and_errors(monkeypatch):
    from wallycull import ai

    monkeypatch.setattr(ai, "Models", lambda config: "models")
    assert ai.Preload({})() == "models"

    def broken(config):
        raise RuntimeError("no GPU")

    monkeypatch.setattr(ai, "Models", broken)
    import pytest

    with pytest.raises(RuntimeError):
        ai.Preload({})()
