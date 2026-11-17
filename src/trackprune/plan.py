"""Decide which tracks to keep and how to flag them. Pure: no I/O, no MKVToolNix.

Policy
------
* Video (and any other non-audio, non-subtitle track) is kept untouched.
* Commentary tracks (audio and subtitles) are dropped, whatever their language.
* Audio: keep the tracks in the original language (or exactly the explicit `audio_ids`, which
  may include commentary), drop the rest. One kept track becomes the default -- the first that
  is not an audio description, unless `default_audio_id` says otherwise.
* Subtitles: keep the tracks in `sub_langs` (English by default), drop the rest.
  - If the kept audio is in a language the viewer reads (one of `sub_langs`), no full
    subtitle track is default; a forced track, if any, becomes forced + default so
    foreign-language lines still show.
  - Otherwise the first full subtitle track becomes default (preferring plain over SDH,
    never a forced or "Signs & Songs" track).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from trackprune.languages import Languages

_FORCED = re.compile(r"\bforced\b|\bforeign\b", re.I)
_SIGNS = re.compile(r"\bsigns?\b|\bsongs?\b", re.I)
_SDH = re.compile(r"\bsdh\b|\bcc\b|hearing.impaired|\bhoh\b", re.I)
_COMMENTARY = re.compile(r"comment", re.I)
_DESCRIPTION = re.compile(r"(?i:\bdescri|visual.impaired)|\bAD\b")


@dataclass
class Track:
    id: int
    type: str  # "video" | "audio" | "subtitles" | "buttons"
    codec: str
    lang: str  # normalised key, see Languages.key
    lang_raw: str
    name: str = ""
    uid: int | None = None
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False
    visual_impaired: bool = False
    commentary: bool = False
    original: bool = False
    channels: int | None = None

    @classmethod
    def from_mkvmerge(cls, t: dict, langs: Languages) -> Track:
        p = t.get("properties", {})
        raw = p.get("language_ietf") or p.get("language") or "und"
        return cls(
            id=t["id"],
            type=t["type"],
            codec=t.get("codec", ""),
            lang=langs.key(raw),
            lang_raw=raw,
            name=p.get("track_name") or "",
            uid=p.get("uid"),
            default=bool(p.get("default_track")),
            forced=bool(p.get("forced_track")),
            hearing_impaired=bool(p.get("flag_hearing_impaired")),
            visual_impaired=bool(p.get("flag_visual_impaired")),
            commentary=bool(p.get("flag_commentary")),
            original=bool(p.get("flag_original")),
            channels=p.get("audio_channels"),
        )

    # Heuristics on top of the flags: release groups often only name the track.
    @property
    def is_commentary(self) -> bool:
        return self.commentary or bool(_COMMENTARY.search(self.name))

    @property
    def is_description(self) -> bool:
        return self.visual_impaired or bool(_DESCRIPTION.search(self.name))

    @property
    def is_forced(self) -> bool:
        return self.forced or bool(_FORCED.search(self.name))

    @property
    def is_signs(self) -> bool:
        return bool(_SIGNS.search(self.name)) and not self.is_forced

    @property
    def is_sdh(self) -> bool:
        return self.hearing_impaired or bool(_SDH.search(self.name))

    @property
    def sub_kind(self) -> str:
        if self.is_forced:
            return "forced"
        if self.is_signs:
            return "signs"
        if self.is_sdh:
            return "sdh"
        return "full"


@dataclass
class Action:
    keep: bool
    default: bool  # the default flag the track ends up with
    forced: bool  # the forced flag the track ends up with
    note: str = ""


@dataclass
class Plan:
    tracks: list[Track]
    actions: dict[int, Action]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> list[Track]:
        return [t for t in self.tracks if not self.actions[t.id].keep]

    @property
    def kept(self) -> list[Track]:
        return [t for t in self.tracks if self.actions[t.id].keep]

    def flag_changes(self, t: Track) -> dict[str, bool]:
        a = self.actions[t.id]
        out = {}
        if a.default != t.default:
            out["default"] = a.default
        if a.forced != t.forced:
            out["forced"] = a.forced
        return out

    @property
    def needs_remux(self) -> bool:
        return bool(self.dropped)

    @property
    def is_noop(self) -> bool:
        return not self.dropped and not any(self.flag_changes(t) for t in self.kept)

    @property
    def ok(self) -> bool:
        return not self.errors


def make_plan(
    tracks: list[Track],
    langs: Languages,
    audio_lang: str | None,
    sub_langs: list[str],
    *,
    audio_ids: list[int] | None = None,
    default_audio_id: int | None = None,
    default_sub_id: int | None = None,
    allow_no_subs: bool = False,
) -> Plan:
    audio_key = langs.key(audio_lang) if audio_lang else None
    sub_keys = [langs.key(s) for s in sub_langs]
    actions: dict[int, Action] = {}
    plan = Plan(tracks, actions)

    for t in tracks:
        if t.type not in ("audio", "subtitles"):
            actions[t.id] = Action(True, t.default, t.forced)

    # --- audio --------------------------------------------------------------------------
    audio = [t for t in tracks if t.type == "audio"]
    if audio_ids is not None:
        unknown = sorted(set(audio_ids) - {t.id for t in audio})
        if unknown:
            plan.errors.append(f"--audio-ids {unknown}: no such audio track")
        kept_audio = [t for t in audio if t.id in audio_ids]
    else:
        kept_audio = [t for t in audio if t.lang == audio_key and not t.is_commentary]

    if audio and not kept_audio:
        present = ", ".join(sorted({t.lang_raw for t in audio}))
        want = audio_lang if audio_ids is None else f"ids {audio_ids}"
        only_commentary = audio_ids is None and any(t.lang == audio_key for t in audio)
        plan.errors.append(
            f"no audio track matches {want}"
            + (" except commentary" if only_commentary else "")
            + f" (file has: {present})"
        )

    if default_audio_id is not None:
        chosen = next((t for t in kept_audio if t.id == default_audio_id), None)
        if chosen is None:
            plan.errors.append(f"--default-audio {default_audio_id} is not a kept audio track")
    else:
        main = [t for t in kept_audio if not t.is_commentary and not t.is_description]
        chosen = (main or kept_audio or [None])[0]
        if len(main) > 1:
            plan.warnings.append(
                f"{len(main)} main audio tracks kept in {audio_key}; "
                f"default is the first (#{chosen.id})"
            )

    for t in audio:
        keep = t in kept_audio
        is_default = keep and t is chosen
        actions[t.id] = Action(keep, is_default, t.forced if keep else False)

    # --- subtitles ----------------------------------------------------------------------
    subs = [t for t in tracks if t.type == "subtitles"]
    kept_subs = [t for t in subs if t.lang in sub_keys and not t.is_commentary]
    understood = chosen is not None and chosen.lang in sub_keys

    default_sub: Track | None = None
    if default_sub_id is not None:
        default_sub = next((t for t in kept_subs if t.id == default_sub_id), None)
        if default_sub is None:
            plan.errors.append(f"--default-sub {default_sub_id} is not a kept subtitle track")
    elif understood:
        default_sub = next((t for t in kept_subs if t.sub_kind == "forced"), None)
    else:
        for kind in ("full", "sdh"):
            default_sub = next((t for t in kept_subs if t.sub_kind == kind), None)
            if default_sub:
                break
        if chosen is not None and default_sub is None:
            lang_names = "/".join(langs.name(k) for k in sub_keys)
            msg = (
                f"audio is {langs.name(chosen.lang)} but there are no full "
                f"{lang_names} subtitles"
            )
            if allow_no_subs:
                plan.warnings.append(msg)
            else:
                plan.errors.append(msg + " (pass --allow-no-subs to proceed anyway)")

    for t in subs:
        keep = t in kept_subs
        if not keep:
            actions[t.id] = Action(False, False, False)
            continue
        is_default = t is default_sub
        # A track that is forced by name gets the flag; the default forced track keeps it.
        forced = t.is_forced
        actions[t.id] = Action(True, is_default, forced, t.sub_kind)

    return plan
