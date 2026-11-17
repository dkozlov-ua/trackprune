"""Thin wrappers over MKVToolNix: identify, remux, edit flags in place."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from trackprune.languages import Languages
from trackprune.plan import Plan, Track

_WINDOWS_DIRS = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "MKVToolNix",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MKVToolNix",
]


class ToolError(RuntimeError):
    pass


@cache
def find_tool(name: str) -> str:
    """Locate an MKVToolNix binary: $TRACKPRUNE_MKVTOOLNIX, PATH, then the usual install dirs."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    override = os.environ.get("TRACKPRUNE_MKVTOOLNIX")
    if override and (Path(override) / exe).is_file():
        return str(Path(override) / exe)
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        for d in _WINDOWS_DIRS:
            if (d / exe).is_file():
                return str(d / exe)
    raise ToolError(
        f"{name} not found. Install MKVToolNix "
        "(Windows: winget install MoritzBunkus.MKVToolNix; Linux: your package manager), "
        "or point TRACKPRUNE_MKVTOOLNIX at its folder."
    )


def _run(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, check=False)


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def version() -> str:
    return _text(_run([find_tool("mkvmerge"), "--version"]).stdout).strip()


@cache
def languages() -> Languages:
    r = _run([find_tool("mkvmerge"), "--list-languages"])
    return Languages.from_mkvmerge_list(_text(r.stdout))


@dataclass
class MediaInfo:
    path: Path
    raw: dict
    tracks: list[Track]

    @property
    def duration_ns(self) -> int | None:
        return self.raw.get("container", {}).get("properties", {}).get("duration")

    @property
    def title(self) -> str:
        return self.raw.get("container", {}).get("properties", {}).get("title") or ""


def identify(path: Path) -> MediaInfo:
    r = _run([find_tool("mkvmerge"), "-J", str(path)])
    try:
        data = json.loads(_text(r.stdout))
    except json.JSONDecodeError as e:
        raise ToolError(f"mkvmerge -J failed: {_text(r.stderr) or e}") from e
    if data.get("errors"):
        raise ToolError("; ".join(data["errors"]))
    container = data.get("container", {})
    if not container.get("recognized") or container.get("type") != "Matroska":
        kind = container.get("type") or "unrecognised"
        raise ToolError(
            f"not a Matroska file (mkvmerge sees: {kind}); incomplete download or wrong format?"
        )
    langs = languages()
    tracks = [Track.from_mkvmerge(t, langs) for t in data.get("tracks", [])]
    return MediaInfo(path, data, tracks)


def _flag(v: bool) -> str:
    return "1" if v else "0"


def remux_args(src: Path, dst: Path, plan: Plan) -> list[str]:
    """mkvmerge arguments that write `dst` with only the kept tracks and the planned flags."""
    kept_audio = [t.id for t in plan.kept if t.type == "audio"]
    kept_subs = [t.id for t in plan.kept if t.type == "subtitles"]
    args = ["--output", str(dst)]
    args += ["--audio-tracks", ",".join(map(str, kept_audio))] if kept_audio else ["--no-audio"]
    args += (
        ["--subtitle-tracks", ",".join(map(str, kept_subs))] if kept_subs else ["--no-subtitles"]
    )
    for t in plan.kept:
        if t.type not in ("audio", "subtitles"):
            continue
        a = plan.actions[t.id]
        args += ["--default-track-flag", f"{t.id}:{_flag(a.default)}"]
        if t.type == "subtitles":
            args += ["--forced-display-flag", f"{t.id}:{_flag(a.forced)}"]
    args.append(str(src))
    return args


def remux(src: Path, dst: Path, plan: Plan, progress: Callable[[int], None] | None = None) -> str:
    """Run mkvmerge; returns its warnings (if any). Raises ToolError on failure."""
    args = [find_tool("mkvmerge"), "--gui-mode", *remux_args(src, dst, plan)]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    messages: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = _text(raw).strip()
        if line.startswith("#GUI#progress "):
            if progress:
                progress(int(line.split()[1].rstrip("%")))
        elif line.startswith(("#GUI#warning", "#GUI#error", "Warning:", "Error:")):
            messages.append(line.removeprefix("#GUI#"))
    code = proc.wait()
    if code >= 2:
        dst.unlink(missing_ok=True)
        raise ToolError("mkvmerge failed: " + " | ".join(messages or ["(no message)"]))
    return " | ".join(messages)


def propedit(path: Path, plan: Plan) -> None:
    """Rewrite only the flags, in place. No remux; seconds even on a 50 GB file."""
    args = [find_tool("mkvpropedit"), str(path)]
    for t in plan.kept:
        changes = plan.flag_changes(t)
        if not changes:
            continue
        if t.uid is None:
            raise ToolError(f"track {t.id} has no UID; cannot address it with mkvpropedit")
        args += ["--edit", f"track:={t.uid}"]
        if "default" in changes:
            args += ["--set", f"flag-default={_flag(changes['default'])}"]
        if "forced" in changes:
            args += ["--set", f"flag-forced={_flag(changes['forced'])}"]
    r = _run(args)
    if r.returncode >= 2:
        raise ToolError("mkvpropedit failed: " + _text(r.stdout + r.stderr).strip())


def verify(dst: Path, plan: Plan, src: MediaInfo) -> MediaInfo:
    """Re-identify the written file and check it holds what the plan promised."""
    out = identify(dst)
    for kind in ("video", "audio", "subtitles"):
        want = sum(1 for t in plan.kept if t.type == kind)
        got = sum(1 for t in out.tracks if t.type == kind)
        if want != got:
            raise ToolError(f"verify: expected {want} {kind} track(s), output has {got}")
    if src.duration_ns and out.duration_ns and abs(src.duration_ns - out.duration_ns) > 1e9:
        raise ToolError(
            f"verify: duration changed {src.duration_ns / 1e9:.1f}s -> {out.duration_ns / 1e9:.1f}s"
        )
    want_defaults = [(t.type, t.lang) for t in plan.kept if plan.actions[t.id].default
                     and t.type in ("audio", "subtitles")]
    got_defaults = [(t.type, t.lang) for t in out.tracks if t.default
                    and t.type in ("audio", "subtitles")]
    if sorted(want_defaults) != sorted(got_defaults):
        raise ToolError(f"verify: default tracks are {got_defaults}, expected {want_defaults}")
    return out
