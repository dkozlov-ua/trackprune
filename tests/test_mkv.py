"""End-to-end against real MKVToolNix, on a tiny synthetic file (PCM audio + SRT subtitles)."""

import subprocess
import wave

import pytest
from typer.testing import CliRunner

from trackprune import mkvtools
from trackprune.cli import app

try:
    MKVMERGE = mkvtools.find_tool("mkvmerge")
except mkvtools.ToolError:
    MKVMERGE = None

pytestmark = pytest.mark.skipif(MKVMERGE is None, reason="MKVToolNix not installed")
runner = CliRunner()


def _wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000 * 3)


def _srt(path, text):
    path.write_text(f"1\n00:00:00,500 --> 00:00:02,000\n{text}\n", encoding="utf-8")


@pytest.fixture
def sample(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _wav(src / "a.wav")
    for lang in ("en", "ru", "fr"):
        _srt(src / f"{lang}.srt", f"hello {lang}")
    args = [MKVMERGE, "-q", "-o", str(src / "Фильм.mkv")]
    for lang, name, default in (("rus", "Дубляж", 1), ("jpn", "Japanese", 0), ("eng", "Eng", 0)):
        args += ["--language", f"0:{lang}", "--track-name", f"0:{name}",
                 "--default-track-flag", f"0:{default}", str(src / "a.wav")]
    for lang in ("eng", "rus", "fre"):
        args += ["--language", f"0:{lang}", "--default-track-flag", "0:0",
                 str(src / f"{lang[:2]}.srt")]
    subprocess.run(args, check=True)
    return src / "Фильм.mkv"


def _summary(path):
    info = mkvtools.identify(path)
    return [(t.type, t.lang, t.default) for t in info.tracks]


def test_out_mode_leaves_source_alone(sample, tmp_path):
    before = sample.read_bytes()
    out = tmp_path / "out"
    r = runner.invoke(app, ["clean", "-a", "ja", "--out", str(out), str(sample)])
    assert r.exit_code == 0, r.output
    assert sample.read_bytes() == before
    assert _summary(out / sample.name) == [("audio", "ja", True), ("subtitles", "en", True)]
    # A second run refuses to clobber the existing output.
    r = runner.invoke(app, ["clean", "-a", "ja", "--out", str(out), str(sample)])
    assert r.exit_code == 1 and "exists" in r.output


def test_in_place_remux_then_flag_edit(sample):
    r = runner.invoke(app, ["clean", "-a", "eng", "--in-place", str(sample)])
    assert r.exit_code == 0, r.output
    assert _summary(sample) == [("audio", "en", True), ("subtitles", "en", False)]
    assert not list(sample.parent.glob("*trackprune-tmp*"))
    # Clear the default flag by hand: now only flags differ, so no remux is needed.
    subprocess.run([mkvtools.find_tool("mkvpropedit"), str(sample), "--edit", "track:a1",
                    "--set", "flag-default=0"], check=True, capture_output=True)
    r = runner.invoke(app, ["clean", "-a", "en", "--in-place", str(sample)])
    assert r.exit_code == 0 and "flags fixed in place" in r.output, r.output
    assert _summary(sample) == [("audio", "en", True), ("subtitles", "en", False)]
    r = runner.invoke(app, ["clean", "-a", "en", "--in-place", str(sample)])
    assert "already clean" in r.output


def test_dry_run_changes_nothing(sample):
    before = sample.read_bytes()
    r = runner.invoke(app, ["clean", "-a", "ja", "--dry-run", str(sample)])
    assert r.exit_code == 0 and "would drop 4" in r.output, r.output
    assert sample.read_bytes() == before


def test_missing_language_fails_without_writing(sample, tmp_path):
    r = runner.invoke(app, ["clean", "-a", "de", "--out", str(tmp_path / "o"), str(sample)])
    assert r.exit_code == 1 and "no audio track matches" in r.output
    assert not (tmp_path / "o").exists()
