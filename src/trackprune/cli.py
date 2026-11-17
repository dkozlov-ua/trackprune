"""trackprune: keep the original-language audio and English subtitles, fix default flags."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

from trackprune import __version__, mkvtools
from trackprune.mkvtools import MediaInfo, ToolError
from trackprune.plan import Plan, Track, make_plan

# Track names are often Cyrillic, CJK, ...; a Windows pipe defaults to cp1252.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console(highlight=False)
err = Console(stderr=True, highlight=False)

TMP_TAG = ".trackprune-tmp"
PATHS_HELP = "MKV files or folders (searched recursively)."
SPACE_MARGIN = 512 * 1024**2


@dataclass
class Job:
    src: Path
    dst: Path | None  # None means in place


def _collect(paths: list[Path], out: Path | None) -> list[Job]:
    jobs: list[Job] = []
    for p in paths:
        p = p.expanduser()
        if p.is_dir():
            files = sorted(
                f for f in p.rglob("*") if f.suffix.lower() in (".mkv", ".mka", ".mks")
                and TMP_TAG not in f.name and f.is_file()
            )
            for f in files:
                dst = out / p.resolve().name / f.relative_to(p) if out else None
                jobs.append(Job(f, dst))
        elif p.is_file():
            jobs.append(Job(p, out / p.name if out else None))
        else:
            raise typer.BadParameter(f"no such file or folder: {p}")
    return jobs


def _flags(t: Track) -> str:
    return " ".join(
        f for f, on in (
            ("default", t.default), ("forced", t.forced), ("SDH", t.hearing_impaired),
            ("commentary", t.commentary), ("AD", t.visual_impaired), ("original", t.original),
        ) if on
    )


def _info_table(info: MediaInfo, plan: Plan | None = None) -> Table:
    tbl = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("id", "type", "lang", "codec", "name", "flags"):
        tbl.add_column(col)
    if plan:
        tbl.add_column("action")
    for t in info.tracks:
        row = [str(t.id), t.type, t.lang_raw, t.codec, escape(t.name), _flags(t)]
        style = None
        if plan:
            a = plan.actions[t.id]
            if not a.keep:
                row.append("[red]drop[/]")
                style = "dim"
            else:
                changes = plan.flag_changes(t)
                desc = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in changes.items())
                row.append(f"[green]keep[/]{' [yellow]' + desc + '[/]' if desc else ''}")
        tbl.add_row(*row, style=style)
    return tbl


def _track_json(t: Track, plan: Plan | None) -> dict:
    d = {
        "id": t.id, "type": t.type, "lang": t.lang, "lang_tag": t.lang_raw, "codec": t.codec,
        "name": t.name, "default": t.default, "forced": t.forced,
        "hearing_impaired": t.hearing_impaired, "commentary": t.commentary,
        "visual_impaired": t.visual_impaired, "original": t.original, "channels": t.channels,
    }
    if t.type == "subtitles":
        d["kind"] = t.sub_kind
    if plan:
        a = plan.actions[t.id]
        d["action"] = {"keep": a.keep, "default": a.default, "forced": a.forced}
    return d


def _parse_ids(s: str | None) -> list[int] | None:
    if s is None:
        return None
    try:
        return [int(x) for x in s.split(",") if x.strip()]
    except ValueError as e:
        raise typer.BadParameter(f"expected comma-separated track ids, got {s!r}") from e


def _version(value: bool) -> None:
    if value:
        console.print(f"trackprune {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    pass


@app.command()
def doctor() -> None:
    """Check that MKVToolNix is installed and usable."""
    try:
        for tool in ("mkvmerge", "mkvpropedit"):
            console.print(f"{tool}: {mkvtools.find_tool(tool)}")
        console.print(mkvtools.version())
        console.print(f"languages known: {len(mkvtools.languages())} codes")
    except ToolError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e


@app.command()
def inspect(
    paths: Annotated[list[Path], typer.Argument(help=PATHS_HELP)],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show tracks, languages and flags. Reads only."""
    results = []
    failed = False
    for job in _collect(paths, None):
        try:
            info = mkvtools.identify(job.src)
        except ToolError as e:
            failed = True
            results.append({"file": str(job.src), "error": str(e)})
            if not as_json:
                err.print(f"[red]{escape(str(job.src))}: {escape(str(e))}[/]")
            continue
        if as_json:
            results.append({
                "file": str(job.src), "title": info.title,
                "duration_s": info.duration_ns / 1e9 if info.duration_ns else None,
                "tracks": [_track_json(t, None) for t in info.tracks],
            })
        else:
            console.print(f"\n[bold]{escape(str(job.src))}[/]"
                          + (f"  [dim]title: {escape(info.title)}[/]" if info.title else ""))
            console.print(_info_table(info))
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    raise typer.Exit(1 if failed else 0)


def _check_space(target_dir: Path, needed: int) -> None:
    free = shutil.disk_usage(target_dir).free
    if free < needed + SPACE_MARGIN:
        raise ToolError(
            f"not enough space in {target_dir}: {free / 1024**3:.1f} GiB free, "
            f"need ~{needed / 1024**3:.1f} GiB"
        )


def _remux_with_progress(src: Path, dst: Path, plan: Plan, quiet: bool) -> str:
    if quiet:
        return mkvtools.remux(src, dst, plan)
    with Progress(
        TextColumn("  remux"), BarColumn(), TaskProgressColumn(), TimeRemainingColumn(),
        console=console, transient=True,
    ) as prog:
        task = prog.add_task("remux", total=100)
        return mkvtools.remux(src, dst, plan, lambda pct: prog.update(task, completed=pct))


def _apply(job: Job, info: MediaInfo, plan: Plan, overwrite: bool, quiet: bool) -> str:
    """Carry out the plan. Returns a short description of what was done."""
    src = job.src
    if job.dst is None:  # in place
        if plan.is_noop:
            return "already clean"
        if not plan.needs_remux:
            mkvtools.propedit(src, plan)
            mkvtools.verify(src, plan, info)
            return "flags fixed in place"
        tmp = src.with_name(src.stem + TMP_TAG + src.suffix)
        _check_space(src.parent, src.stat().st_size)
        try:
            warn = _remux_with_progress(src, tmp, plan, quiet)
            mkvtools.verify(tmp, plan, info)
            os.replace(tmp, src)
        finally:
            tmp.unlink(missing_ok=True)
        return "remuxed in place" + (f" (mkvmerge: {warn})" if warn else "")

    dst = job.dst
    if dst.resolve() == src.resolve():
        raise ToolError("output path is the source file; use --in-place instead")
    if dst.exists() and not overwrite:
        raise ToolError(f"{dst} exists (pass --overwrite to replace it)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + TMP_TAG + dst.suffix)
    _check_space(dst.parent, src.stat().st_size)
    try:
        warn = _remux_with_progress(src, tmp, plan, quiet)
        mkvtools.verify(tmp, plan, info)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return f"written to {dst}" + (f" (mkvmerge: {warn})" if warn else "")


@app.command()
def clean(
    paths: Annotated[list[Path], typer.Argument(help=PATHS_HELP)],
    audio: Annotated[str | None, typer.Option(
        "--audio", "-a", help="Original language to keep, e.g. eng, en, jpn, ja.")] = None,
    subs: Annotated[list[str] | None, typer.Option(
        "--subs", "-s", help="Subtitle language(s) to keep. Repeatable. Default: eng.")] = None,
    out: Annotated[Path | None, typer.Option(
        "--out", "-o", help="Write cleaned copies here; sources stay untouched.")] = None,
    in_place: Annotated[bool, typer.Option(
        "--in-place", help="Replace the source files (remux to a temp file, verify, swap).")
    ] = False,
    audio_ids: Annotated[str | None, typer.Option(
        "--audio-ids", help="Keep exactly these audio track ids (comma-separated) "
        "instead of matching --audio. For mislabelled tracks.")] = None,
    default_audio: Annotated[int | None, typer.Option(
        "--default-audio", help="Track id of the audio track to make default.")] = None,
    default_sub: Annotated[int | None, typer.Option(
        "--default-sub", help="Track id of the subtitle track to make default.")] = None,
    allow_no_subs: Annotated[bool, typer.Option(
        "--allow-no-subs", help="Proceed when the audio is foreign and no subtitles are kept.")
    ] = False,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", "-n", help="Show the plan; change nothing.")] = False,
    overwrite: Annotated[bool, typer.Option(
        "--overwrite", help="Replace existing files in --out.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Drop non-original audio and foreign subtitles; set the default flags."""
    if audio is None and audio_ids is None:
        raise typer.BadParameter("pass --audio LANG (or --audio-ids)")
    if not dry_run and (out is None) == (not in_place):
        raise typer.BadParameter("choose exactly one of --out DIR or --in-place")
    sub_langs = subs or ["eng"]
    try:
        langs = mkvtools.languages()
    except ToolError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(2) from e
    for code in [c for c in [audio, *sub_langs] if c]:
        if not langs.known(code):
            raise typer.BadParameter(f"unknown language code {code!r}")
    ids = _parse_ids(audio_ids)

    results = []
    failed = 0
    jobs = _collect(paths, None if dry_run and out is None else out)
    for n, job in enumerate(jobs, 1):
        rec: dict = {"file": str(job.src)}
        results.append(rec)
        quiet = as_json
        if not quiet:
            console.print(f"\n[bold]\\[{n}/{len(jobs)}] {escape(str(job.src))}[/]")
        try:
            info = mkvtools.identify(job.src)
            plan = make_plan(
                info.tracks, langs, audio, sub_langs, audio_ids=ids,
                default_audio_id=default_audio, default_sub_id=default_sub,
                allow_no_subs=allow_no_subs,
            )
            rec["tracks"] = [_track_json(t, plan) for t in info.tracks]
            rec["warnings"] = plan.warnings
            if not quiet:
                console.print(_info_table(info, plan))
                for w in plan.warnings:
                    console.print(f"  [yellow]warning:[/] {escape(w)}")
            if not plan.ok:
                raise ToolError("; ".join(plan.errors))
            if dry_run:
                rec["result"] = "dry run: " + (
                    "already clean" if plan.is_noop
                    else f"would drop {len(plan.dropped)} track(s)" if plan.needs_remux
                    else "would fix flags only"
                )
            else:
                rec["result"] = _apply(job, info, plan, overwrite, quiet)
            rec["ok"] = True
            if not quiet:
                console.print(f"  [green]{escape(rec['result'])}[/]")
        except (ToolError, OSError) as e:
            failed += 1
            rec["ok"] = False
            rec["error"] = str(e)
            if not quiet:
                console.print(f"  [red]error:[/] {escape(str(e))}")
        except KeyboardInterrupt:
            err.print("[red]interrupted[/]")
            raise typer.Exit(130) from None

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        tail = f", {failed} failed" if failed else ""
        console.print(f"\n{len(jobs) - failed}/{len(jobs)} ok{tail}")
    raise typer.Exit(1 if failed else 0)
