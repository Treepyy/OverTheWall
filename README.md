# overthewall

Convert **WGZSkins** custom League of Legends skins (`.lolgezi` files, as distributed by the
"lolgezi" / WGZSkins tool common on the Chinese market) into the **`.fantome`** format used by
modern skin managers such as LTK Manager.

The tool is a single self-contained Python script with a small command-line interface. It does a
**faithful repackage** of the skin: it unwraps the proprietary container, recovers the League WAD
hidden inside, restores its header, splits out localized voice-over so foreign voices load on a
global client, and rewraps everything as a standard `.fantome` archive.

---

## Table of contents

- [Scope: what this tool does and does not do](#scope)
- [Background: the two formats](#background)
- [Conversion methodology](#methodology)
- [How the program works, function by function](#how-it-works)
- [Installation](#installation)
- [Usage](#usage)
- [Verbose diagnostics](#verbose)
- [Cross-champion textures (white-box VFX)](#cross-champion)
- [Limitations and future work](#limitations)

---

<a name="scope"></a>
## Scope: what this tool does and does not do

**It does:** unwrap a `.lolgezi`, recover and validate the embedded WAD, detect the champion,
restore the WAD header, route localized voice banks into the correct locale WAD, and produce a
ready-to-install `.fantome`.

**It does not:** fix patch compatibility. Conversion only changes the *container*; it does not touch
the skin's internal data. A skin authored for an older game patch can still crash on the current
patch (because its bins reference objects that Riot has since renamed, moved, or restructured).
Making such a skin load again is a separate, manual *porting* effort — case-by-case bin surgery that
is not automatable. Conversion alone is correct whenever the source skin already targets the current
patch, which is the case for any "recently updated" skin.

---

<a name="background"></a>
## Background: the two formats

### WGZSkins (`.lolgezi`)

A `.lolgezi` file is a proprietary container with an 8-byte magic `WGZSkins`, a fixed 64-byte header,
and then a sequence of independent **Zstandard** frames. In every file observed, the frames are:

| Frame | Contents |
|-------|----------|
| 0 | A manifest (a long hex string) |
| 1 | A JPEG preview / splash image |
| 2 | The actual skin payload — a standard League **WAD v3** |

The crucial quirk: the embedded WAD has its **first 268 bytes zeroed** (the `RW` magic, the ECDSA
signature, and the checksum block are wiped). Some files leave a few stray signature bytes, so the
WAD **cannot** be found by looking for the `RW` magic or by scanning for a run of zeros — it has to be
identified by its *table structure* (see [methodology](#methodology)).

### League WAD v3

A WAD is League's asset archive. After the 268-byte header it stores:

| Offset | Field | Type |
|--------|-------|------|
| 268 | entry count | `uint32` |
| 272 | table of contents | `entryCount` × 32-byte entries |
| 272 + count·32 | chunk data | contiguous blob |

Each 32-byte TOC entry is:

| Bytes | Field |
|-------|-------|
| 0–7 | path hash (`xxh64` of the lowercased asset path, seed 0) |
| 8–11 | data offset |
| 12–15 | compressed size |
| 16–19 | uncompressed size |
| 20 | storage type (low nibble) |
| 21 | subchunk count |
| 22–23 | padding |
| 24–31 | checksum |

Storage types seen in the wild: `0` (stored / raw — used for audio banks and large meshes) and
`3` (Zstandard). The tool also handles `1` (gzip) and `4` (Zstandard split across subchunks)
defensively, even though no sample has used them yet.

### `.fantome`

A `.fantome` is just a ZIP archive:

```
META/info.json              # mod metadata (Name, Author, Version, Description)
META/image.png              # preview image (optional)
WAD/<Champion>.wad.client   # the skin's WAD
WAD/<Champion>.<locale>.wad.client   # optional, for localized voice-over
```

The skin manager overlays each `WAD/<name>.wad.client` on top of the game's identically named WAD,
matching entries by their path hash. This is why voice-over has to live in the *locale* WAD: the
game serves voice from `<Champion>.<locale>.wad.client`, so a VO bank placed in the plain champion
WAD would never override anything.

---

<a name="methodology"></a>
## Conversion methodology

The full pipeline, step by step:

1. **Verify the container.** Check the `WGZSkins` magic.

2. **Extract the Zstandard frames.** Locate the first zstd frame after the 64-byte header, then walk
   frame by frame, using each decompressor's leftover bytes to find where the next frame begins.

3. **Find the WAD by structure.** For each frame, read the `uint32` at offset 268 as a candidate
   entry count; sanity-check it; then confirm that every TOC entry's data offset points at or past
   the end of the TOC. Only a real WAD passes. The JPEG preview is found by its `FF D8 FF` magic.

4. **Validate.** Decompress every chunk and confirm it inflates to its recorded uncompressed size.
   A healthy conversion validates 100% of chunks.

5. **Detect the champion.** Scan the decompressed bins for `Characters/<Name>/...` references and take
   a **majority vote**. This is necessary because a skin physically duplicates shared particle
   textures from other champions, so a naive "first match" would sometimes pick the wrong name. A
   reference to the champion's own root bin (`Characters/X/X.bin`) counts double as a strong signal.
   The exact casing is preserved for the output WAD filename.

6. **Restore the WAD header.** Write back `RW`, major version `3`, minor version `3`. The signature
   and checksum are left zeroed — the game and skin managers accept this.

7. **Split localized voice-over.** Scan the bins for audio-bank paths. Any bank under
   `Sounds/Wwise2016/VO/<locale>/...` is peeled out of the champion WAD and placed into
   `<Champion>.<locale>.wad.client`. Sound effects (no locale segment) stay in the champion WAD.
   This is what makes, say, a Japanese-voice skin actually speak Japanese on a global client (the
   author bundles the JP audio at `en_US` paths so it overrides the English voice).

8. **Repackage.** Reassemble each WAD (header + recomputed TOC + chunk data, copying chunk bytes
   verbatim) and zip them with `info.json` and the converted preview into a `.fantome`. Output
   filenames are stripped to ASCII, because non-ASCII (e.g. CJK) filenames break downloads in some
   environments.

---

<a name="how-it-works"></a>
## How the program works, function by function

### Module-level constants

- `WGZ_MAGIC`, `ZSTD_MAGIC`, `JPEG_MAGIC` — the signatures used to identify the container, frames,
  and preview.
- `KNOWN_LOCALES` — the set of League locale codes, used to normalize the casing of a detected
  voice-over locale.
- `_SKINS_RE`, `_ROOTBIN_RE`, `_BANK_RE`, `_VO_LOCALE_RE`, `_ASSET_REF_RE`, `_SKIN_SEG_RE` —
  precompiled regexes for scanning bin bytes (champion references, audio bank paths, voice-over
  locale, generic asset references, and the `Skins/<segment>/` path component).

### `xxh64(text) -> int`

The path-hashing primitive: `xxh64` of the **lowercased** path string with seed 0. Every WAD entry
is keyed by this, so it is also how the tool checks whether a referenced asset is bundled.

### `read_frames(data) -> list[bytes]`

Decompresses every top-level Zstandard frame in the container. It finds the first frame after the
header, then advances by the number of bytes each frame actually consumed (`total - unused_data`),
skipping any inter-frame padding. Returns the list of decompressed frame payloads.

### `looks_like_wad(buf) -> bool`

Structural WAD detector. Reads the candidate entry count at offset 268, bounds-checks it, and
verifies that the minimum chunk data offset across the whole TOC lands at or after the end of the
TOC. This is what lets the tool recognize a WAD whose magic has been zeroed out.

### `find_wad_and_preview(frames) -> (wad, preview)`

Picks the first WAD-structured frame and the first JPEG frame from the list. Raises if no WAD is
found; the preview is optional and may be `None`.

### `class Chunk`

A lightweight holder (with `__slots__`) for one WAD entry: its `path_hash`, the raw (still-compressed)
`raw` bytes, `csize`, `usize`, storage `ctype`, and `checksum`.

### `read_chunks(wad) -> list[Chunk]`

Parses the WAD's TOC into a list of `Chunk` objects, slicing each chunk's raw bytes out of the WAD.

### `decompress_chunk(chunk, dctx) -> bytes`

Inflates a single chunk according to its storage type: `0` returns the bytes as-is, `1` gunzips,
`3`/`2` zstd-decompresses, and `4` zstd-decompresses across subchunk frames.

### `validate(chunks) -> (valid, total, failed_hashes)`

Decompresses every chunk and counts how many inflate to their recorded uncompressed size. Returns the
valid count, the total, and the list of path-hashes that failed — surfaced in verbose mode.

### `_iter_decompressed(chunks, dctx)`

Internal generator that yields each chunk's decompressed bytes, skipping any that fail. Shared by the
three scanning passes below.

### `detect_champion(chunks) -> (name, votes)`

Runs the majority-vote champion detection described in the methodology and returns both the
exact-case champion name and the full `Counter` of votes (so verbose mode can show the tally and any
runner-up noise).

### `detect_locale_banks(chunks) -> (by_locale_hashes, banks)`

Scans bins for audio-bank paths, keeps those that contain a `VO/<locale>/` segment **and** are
actually present in this WAD, and groups their path-hashes by (canonicalized) locale. Returns the
hash sets used to split the WAD, plus a sorted `(locale, path)` list for reporting.

### `audit_references(chunks) -> (by_ext, primary_skin)`

Scans bins for every asset reference (`.dds`, `.tex`, `.skn`, `.skl`, `.anm`, `.bnk`, `.wpk`,
`.scb`, `.sco`) and tallies, per extension, how many are bundled versus not bundled. It also
determines the skin's **own** skin path (`primary_skin`) — the `Skins/<segment>/` that most *bundled*
assets live under. This lets verbose mode distinguish a genuinely missing custom asset (a red flag,
e.g. a missing texture causing white-box particles) from the many base-game/shared references a skin
legitimately reuses (normal, not a problem).

### `build_wad(chunks) -> bytes`

Reassembles a valid WAD v3.3 from a list of chunks: writes the restored header, a freshly computed
TOC with recalculated data offsets, and the concatenated chunk bytes (copied verbatim, preserving
each chunk's original compression and checksum).

### `ascii_safe(stem, fallback) -> str`

Reduces a filename stem to `[A-Za-z0-9._-]`, collapsing the rest to underscores, with a fallback
(the champion name) if nothing usable remains.

### `package_fantome(out_path, wads, info, preview)`

Writes the final ZIP: `META/info.json`, the converted `META/image.png` (if Pillow is available and a
preview exists), and each WAD under `WAD/` (stored without additional ZIP compression, since WAD
contents are already compressed).

### `convert(src_path, out_dir, ...)`

Orchestrates a single conversion end to end: read → extract frames → find WAD/preview → read chunks →
validate → detect champion → split locale banks → build WADs → package. Emits the normal one-line
summary, and — when `verbose=True` — the full diagnostics. Honors `quiet`.

### `gather_inputs(paths) -> list[str]`

Expands the CLI inputs: a directory contributes all the `.lolgezi` files inside it; a file is taken
as-is. This is what enables batch conversion.

### `main(argv=None) -> int`

Parses arguments, gathers inputs, converts each (reporting per-file failures without aborting the
batch), and returns a process exit code (`0` only if everything converted).

---

<a name="installation"></a>
## Installation

Requires **Python 3.8+** and two packages (a third is optional):

```bash
pip install zstandard xxhash pillow
```

- `zstandard` — decompress the container frames and WAD chunks. **Required.**
- `xxhash` — compute WAD path hashes. **Required.**
- `pillow` — convert the JPEG preview to PNG for the `.fantome`. **Optional**; without it, the mod is
  built without a preview image.
- `pyRitoFile` — re-serialize bins for the `--fix-cross-champion` pass. **Optional**; only needed if
  you use that flag (`pip install pyRitoFile`).

Then just keep `overthewall.py` somewhere on your path. There is nothing to build.

---

<a name="usage"></a>
## Usage

```bash
# Convert a single skin into the current directory
python overthewall.py skin.lolgezi

# Convert several at once into an output folder
python overthewall.py a.lolgezi b.lolgezi -o converted/

# Batch-convert every .lolgezi in a folder
python overthewall.py /path/to/skins/ -o converted/
```

### Options

| Flag | Effect |
|------|--------|
| `-o`, `--output-dir DIR` | Where to write `.fantome` files (default: current directory). |
| `--name NAME` | Override the mod name and output filename (single-input only). |
| `--author AUTHOR` | Set the author field in `info.json`. |
| `--no-split-vo` | Keep voice banks in the champion WAD instead of splitting them into a locale WAD. |
| `--no-preview` | Skip embedding the splash preview image. |
| `--fix-cross-champion` | Localize particle textures borrowed from other champions to host-local paths, fixing white-box VFX (requires `pyRitoFile`). |
| `-v`, `--verbose` | Print detailed diagnostics (see below). |
| `-q`, `--quiet` | Suppress all output (overrides `--verbose`). |

### Example (normal output)

```
[*] yorick_mutsu_cn里克-若-睦_日语_.lolgezi
  champion: Yorick   chunks: 375/375 valid
  locale wad: Yorick.en_US.wad.client (3 banks)
  -> converted/yorick_mutsu_cn.fantome  (44.7 MB)

Done: 1/1 converted.
```

---

<a name="verbose"></a>
## Verbose diagnostics

`-v` adds a per-skin diagnostic block:

```
    container: 45.7 MB, 3 frames; WAD frame 60.6 MB; preview: yes
    chunk types: stored=25, zstd=965
  champion: Yone   chunks: 990/990 valid
    champion votes: yone:8323, syndra:59, annie:57, diana:57, anivia:42
    voice banks (-> locale wad):
      [en_US] Yone_Skin55_VO_audio.bnk
      [en_US] Yone_Skin55_VO_audio.wpk
      [en_US] Yone_Skin55_VO_events.bnk
  locale wad: Yone.en_US.wad.client (3 banks)
    reference audit (bundled / not-bundled by type); skin's own path: Skins/Skin55/:
      .anm: 83 bundled, 268 from base game
      .dds: 7 bundled, 0 from base game
      .scb: 66 bundled, 0 from base game
      .skl: 5 bundled, 0 from base game
      .skn: 5 bundled, 0 from base game
      .tex: 340 bundled, 0 from base game
    cross-champion refs: 44 (44 bundled at FOREIGN paths -> white-box risk) from Yasuo:12, Annie:11, Anivia:10, Diana:9, Sett:1
```

What to read from it:

- **container / chunk types** — quick shape of the file and its compression mix.
- **champion votes** — confirms the detected champion really dominates; the small runner-up counts
  are shared particle references from other champions and are expected.
- **voice banks** — exactly which banks were routed to which locale WAD.
- **reference audit** — for each asset type, how many references are bundled versus drawn from the
  base game. The "skin's own path" line names the skin segment the mod's assets live under. A
  missing reference is only flagged with a `CHECK` line **when it falls under the skin's own path** —
  that is the signal for a genuinely absent custom asset (e.g. a missing particle texture, the usual
  cause of white-box VFX). Missing base-game/shared references are reported as a plain count and are
  normal.
- **cross-champion refs** — particle assets the skin borrows from *other* champions (see the section
  below). This is reported in verbose, and as a one-line note in normal output, whenever present.

---

<a name="cross-champion"></a>
## Cross-champion textures (white-box VFX)

Many skins are "patchwork" builds whose effects reuse particles from other champions — a frost
footstep from Diana, a slash trail from Yasuo, and so on. The packer bundles those borrowed textures
**at their original foreign paths** (e.g. `ASSETS/Characters/Diana/Skins/Skin47/Particles/...`). The
bytes are physically present in your champion WAD, so they don't show up as "missing" — but the game
resolves a `Characters/Diana/...` asset from **Diana's** WAD, which isn't loaded unless a Diana is in
the match. The particle therefore can't bind its texture and renders as a blank **white box / quad**.
This is a render-level failure, not a missing file, which is why it survives the normal audit.

The tool detects this automatically (counting references whose `Characters/<X>/` segment isn't the
host champion) and reports it. Passing `--fix-cross-champion` resolves it: for every such texture it

1. computes a host-local path (`ASSETS/Characters/<Host>/xport/<flattened-original-path>`),
2. rewrites every reference in the bins to point at the new path (using `pyRitoFile` to re-serialize,
   since changing a path's length shifts nested size fields a byte-patch can't), and
3. re-adds the texture bytes under the new host-local hash.

After the pass, those textures resolve from the host champion's own WAD — which is always loaded when
that champion is played — so the effects render correctly. The detection itself needs no extra
dependency; only the `--fix-cross-champion` rewrite requires `pyRitoFile`.

---

<a name="limitations"></a>
## Limitations and future work

- **No patch-compatibility check.** The tool cannot currently tell you whether a converted skin will actually load on the live patch. Diff'ing the skin's referenced objects against your installed game WADs (which requires a configurable path to the game) and flag any that have been renamed or moved will be added later (probably).
- **Conversion, not porting.** Skins built for an older patch may still crash; repairing them is manual bin work outside this tool's scope.
- **Locale detection is voice-over only.** Only `VO/<locale>/` banks are split out; sound effects and other assets always go in the champion WAD (which is correct).
- **Heuristic champion detection.** Detection is by majority vote over plaintext references, not a manifest lookup, so an unusually shared-asset-heavy skin could in principle mis-vote; the verbose tally exists to make that visible.
- **Cross-champion fix is texture-scoped.** `--fix-cross-champion` localizes texture/particle assets
  (`.tex/.dds/.scb/.sco`); it does not attempt to re-path other borrowed asset classes, which are
  rarely the cause of visible breakage.
