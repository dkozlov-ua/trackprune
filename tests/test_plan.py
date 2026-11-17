import pytest

from trackprune.languages import Languages
from trackprune.plan import Track, make_plan

LIST = """\
English language name | ISO 639-3 code | ISO 639-2 code | ISO 639-1 code
----------------------+----------------+----------------+---------------
English               | eng            | eng            | en
Japanese              | jpn            | jpn            | ja
Russian               | rus            | rus            | ru
Ukrainian             | ukr            | ukr            | uk
French                | fre            | fre            | fr
Cantonese             | yue            |                |
"""
LANGS = Languages.from_mkvmerge_list(LIST)


def T(id, type, lang, name="", **kw):
    return Track(id=id, type=type, codec="x", lang=LANGS.key(lang), lang_raw=lang, name=name, **kw)


@pytest.mark.parametrize(
    "code,key",
    [("eng", "en"), ("en", "en"), ("en-US", "en"), ("ENG", "en"), ("fra", "fr"), ("fre", "fr"),
     ("yue", "yue"), ("und", "und"), (None, "und"), ("zz", "zz")],
)
def test_language_keys(code, key):
    assert LANGS.key(code) == key


def movie_with_dubs():
    # Shaped like a typical multi-dub WEB-DL: dub first and default, original last.
    return [
        T(0, "video", "en", default=True),
        T(1, "audio", "ru", "Dub", default=True),
        T(2, "audio", "ru", "DVO"),
        T(3, "audio", "uk", "Dub"),
        T(4, "audio", "en"),
        T(5, "subtitles", "ru", "forced", default=True),
        T(6, "subtitles", "ru", "full"),
        T(7, "subtitles", "en-US", "full"),
        T(8, "subtitles", "en", "SDH", hearing_impaired=True),
    ]


def test_english_movie_keeps_english_audio_default_and_no_default_subs():
    p = make_plan(movie_with_dubs(), LANGS, "eng", ["eng"])
    assert p.ok
    assert [t.id for t in p.kept] == [0, 4, 7, 8]
    assert p.actions[4].default and p.actions[0].keep
    assert not p.actions[7].default and not p.actions[8].default
    assert p.flag_changes(next(t for t in p.tracks if t.id == 4)) == {"default": True}
    assert p.needs_remux


def test_english_movie_forced_english_subs_become_default_and_forced():
    tracks = movie_with_dubs() + [T(9, "subtitles", "en", "Forced")]
    p = make_plan(tracks, LANGS, "en", ["en"])
    assert p.actions[9].default and p.actions[9].forced
    assert not p.actions[7].default


def test_anime_keeps_japanese_and_defaults_full_english_subs():
    tracks = [
        T(0, "video", "ja", default=True),
        T(1, "audio", "ja", "Japanese", default=True),
        T(2, "audio", "en", "English dub"),
        T(3, "subtitles", "en", "Signs & Songs"),
        T(4, "subtitles", "en", "English"),
        T(5, "subtitles", "fr", "French"),
    ]
    p = make_plan(tracks, LANGS, "jpn", ["eng"])
    assert p.ok
    assert [t.id for t in p.kept] == [0, 1, 3, 4]
    assert p.actions[1].default
    assert p.actions[4].default and not p.actions[3].default


def test_full_subs_preferred_over_sdh_and_sdh_used_as_fallback():
    base = [T(0, "video", "ja"), T(1, "audio", "ja")]
    p = make_plan(base + [T(2, "subtitles", "en", "SDH"), T(3, "subtitles", "en", "Full")],
                  LANGS, "ja", ["en"])
    assert p.actions[3].default and not p.actions[2].default
    p = make_plan(base + [T(2, "subtitles", "en", "SDH")], LANGS, "ja", ["en"])
    assert p.actions[2].default


def test_foreign_audio_without_english_subs_is_an_error_unless_allowed():
    tracks = [T(0, "video", "ja"), T(1, "audio", "ja"), T(2, "subtitles", "fr")]
    assert not make_plan(tracks, LANGS, "ja", ["en"]).ok
    p = make_plan(tracks, LANGS, "ja", ["en"], allow_no_subs=True)
    assert p.ok and p.warnings and [t.id for t in p.kept] == [0, 1]


def test_signs_only_is_not_enough_for_foreign_audio():
    tracks = [T(0, "video", "ja"), T(1, "audio", "ja"), T(2, "subtitles", "en", "Signs/Songs")]
    assert not make_plan(tracks, LANGS, "ja", ["en"]).ok


def test_missing_native_audio_is_an_error():
    p = make_plan(movie_with_dubs(), LANGS, "jpn", ["eng"])
    assert not p.ok and "no audio track matches" in p.errors[0]


def test_commentary_dropped_and_description_kept_but_not_default():
    tracks = [
        T(0, "video", "en"),
        T(1, "audio", "en", "Director's Commentary", default=True),
        T(2, "audio", "en", "Audio Description"),
        T(3, "audio", "en", "Main"),
        T(4, "audio", "en", commentary=True),
        T(5, "subtitles", "en", "English"),
        T(6, "subtitles", "en", "Commentary by the director"),
    ]
    p = make_plan(tracks, LANGS, "en", ["en"])
    assert [t.id for t in p.kept] == [0, 2, 3, 5]
    assert [i for i, a in p.actions.items() if a.default] == [3]


def test_only_commentary_in_language_is_an_error_but_ids_can_keep_it():
    tracks = [T(0, "video", "en"), T(1, "audio", "ru"), T(2, "audio", "en", "Commentary")]
    p = make_plan(tracks, LANGS, "en", ["en"])
    assert not p.ok and "except commentary" in p.errors[0]
    p = make_plan(tracks, LANGS, None, ["en"], audio_ids=[2])
    assert p.ok and [t.id for t in p.kept] == [0, 2]


def test_explicit_ids_override_language():
    tracks = [T(0, "video", "und"), T(1, "audio", "und"), T(2, "audio", "ru"),
              T(3, "subtitles", "en")]
    p = make_plan(tracks, LANGS, None, ["en"], audio_ids=[1], allow_no_subs=True)
    assert p.ok and [t.id for t in p.kept] == [0, 1, 3]
    assert p.actions[1].default


def test_bad_default_ids_are_errors():
    p = make_plan(movie_with_dubs(), LANGS, "en", ["en"], default_audio_id=1, default_sub_id=6)
    assert len(p.errors) == 2


def test_already_clean_is_noop():
    tracks = [T(0, "video", "en", default=True), T(1, "audio", "en", default=True),
              T(2, "subtitles", "en")]
    p = make_plan(tracks, LANGS, "en", ["en"])
    assert p.is_noop


def test_flags_only_change_needs_no_remux():
    tracks = [T(0, "video", "ja"), T(1, "audio", "ja"), T(2, "subtitles", "en")]
    p = make_plan(tracks, LANGS, "ja", ["en"])
    assert not p.needs_remux and not p.is_noop
