# trackprune

Strip a video file down to the tracks you actually watch: the **original-language audio** and
**English subtitles** (or whichever languages you read), with the default flags set so a
player picks the right ones by itself.

Multi-dub releases often ship five dubs ahead of the original and make one of them the
default. Fansub releases carry ten subtitle languages. trackprune remuxes them losslessly with
[MKVToolNix](https://mkvtoolnix.download/): no re-encoding, and video (including Dolby Vision and
HDR metadata), chapters, tags and attachments (fonts for styled subtitles) are kept intact.

trackprune does not guess which language is "original" -- you tell it (`--audio jpn`). It
shows you every track first, so the call is easy to make.

## Install

Requires [MKVToolNix](https://mkvtoolnix.download/) (`winget install MoritzBunkus.MKVToolNix`,
`apt install mkvtoolnix`, `brew install mkvtoolnix`) and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/dkozlov-ua/trackprune
trackprune doctor            # finds mkvmerge / mkvpropedit
uv tool upgrade trackprune   # later, to update
```

MKVToolNix is looked up on `PATH`, then in its default Windows install folders; set
`TRACKPRUNE_MKVTOOLNIX` to its folder to override.

## Usage

```sh
trackprune inspect "Movie.2006.mkv"                   # list tracks; reads only
trackprune clean -a eng --dry-run "Movie.2006.mkv"    # show the plan
trackprune clean -a eng --out D:\Clean "Movie.2006.mkv"
trackprune clean -a jpn --in-place "Some Show (Season 01)"   # a whole folder
```

| command | what it does |
|---|---|
| `inspect <path>...` | Tracks, languages, names and flags. `--json` for machine output. |
| `clean <path>... -a LANG` | Keep audio in `LANG`, subtitles in `--subs` (default `eng`); set defaults. |
| `doctor` | Check that MKVToolNix is installed. |

Folders are searched recursively for `.mkv`. Language codes may be ISO 639-1, 639-2 or BCP 47:
`en`, `eng` and `en-US` are the same language.

### Where the result goes

You must pick one:

- `--out DIR` writes cleaned copies to `DIR` and leaves the sources untouched (a folder
  argument keeps its own name and layout under `DIR`). Use this for files that are still
  seeding: rewriting a torrent's files makes them fail its hash check.
- `--in-place` replaces each source: remux to a temp file beside it, verify it, then swap it
  in. When only flags need changing, `mkvpropedit` edits them in place in seconds.

Every written file is re-read and checked: track counts, duration, and default flags.
Existing files in `--out` are not overwritten without `--overwrite`.

### The rules

**Commentary.** Commentary tracks, audio and subtitles, are dropped in every language.

**Audio.** Tracks in the `--audio` language are kept; all others are dropped. One kept track
becomes the default: the first that is not an audio description.

**Subtitles.** Tracks in the `--subs` languages are kept; all others are dropped. Then:

- If the audio is in a language you read (one of `--subs`), no full subtitle track is the
  default. A *forced* track (foreign-dialogue lines only) becomes forced + default.
- Otherwise the first full subtitle track becomes the default, preferring plain over SDH and
  never a forced or "Signs & Songs" track. If there is no full track in your languages,
  trackprune refuses the file unless you pass `--allow-no-subs`.

Forced, SDH, commentary and description tracks are recognised by their flags and, since
releases often only name them, by their track names.

### Overrides

| option | use |
|---|---|
| `--audio-ids 1,4` | Keep exactly these audio tracks (mislabelled or `und` languages; may include commentary). |
| `--default-audio ID` | Pick the default audio track yourself. |
| `--default-sub ID` | Pick the default subtitle track yourself. |
| `--subs eng --subs ukr` | Keep subtitles in several languages. |

Track ids are the ones `inspect` prints (mkvmerge's ids).

## Development

```sh
uv sync
uv run pytest      # the end-to-end tests need MKVToolNix; they build tiny files themselves
uv run ruff check .
```

## License

MIT
