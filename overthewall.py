#!/usr/bin/env python3
"""
overthewall - convert WGZSkins (.lolgezi) custom skins into .fantome mods.

Pipeline:
  1. Parses the WGZSkins container and pulls out its zstd frames.
  2. Locates the embedded League WAD *by structure* (its header is zeroed, so
     we cannot rely on the "RW" magic or a zero-run) and the JPEG preview.
  3. Validates every chunk (decompresses and checks the uncompressed size).
  4. Auto-detects the champion from the bin plaintext (majority vote, so shared
     particle references from other champions don't win).
  5. Restores the WAD header (RW / v3.3) and, if the skin carries voice-over
     banks at a locale path, peels those entries into <Champion>.<locale>.wad.client
     so foreign VO actually loads on a global client.
  6. Repackages everything as a .fantome zip with an ASCII-safe filename.

Conversion is a faithful repackage only. It does **NOT** fix patch-compatibility;
a skin built for an older patch can still crash and must be ported separately.

Dependencies: zstandard, xxhash, pillow (pillow optional, only for the preview).
Usage:
    python overthewall.py skin.lolgezi [more.lolgezi ...] [-o OUTDIR]
    python overthewall.py /path/to/folder -o OUTDIR      # batch a directory
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import struct
import sys
import zipfile
from collections import Counter

import zstandard as zstd
import xxhash

try:
    from PIL import Image
except ImportError:  # preview is a nice-to-have, not required
    Image = None

WGZ_MAGIC = b"WGZSkins"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
JPEG_MAGIC = b"\xff\xd8\xff"
KNOWN_LOCALES = {
    "en_US", "en_GB", "en_AU", "en_PH", "en_SG", "ja_JP", "ko_KR", "zh_CN",
    "zh_TW", "zh_MY", "es_ES", "es_MX", "es_AR", "fr_FR", "de_DE", "it_IT",
    "pl_PL", "ro_RO", "el_GR", "pt_BR", "hu_HU", "ru_RU", "tr_TR", "cs_CZ",
    "th_TH", "vi_VN", "id_ID",
}


def xxh64(text: str) -> int:
    """League hashes WAD paths as xxh64 of the lowercased path (seed 0)."""
    return xxhash.xxh64(text.lower().encode("utf-8"), seed=0).intdigest()


# --------------------------------------------------------------------------- #
# WGZSkins container
# --------------------------------------------------------------------------- #
def read_frames(data: bytes) -> list[bytes]:
    """Decompress every top-level zstd frame in a WGZSkins payload."""
    dctx = zstd.ZstdDecompressor()
    frames: list[bytes] = []
    pos = data.find(ZSTD_MAGIC, 64)  # frames start after the 64-byte header
    if pos < 0:
        raise ValueError("no zstd frames found in container")
    while 0 <= pos < len(data):
        dobj = dctx.decompressobj(read_across_frames=False)
        out = dobj.decompress(data[pos:])
        consumed = (len(data) - pos) - len(dobj.unused_data)
        frames.append(out)
        pos += consumed
        if pos < len(data) and data[pos:pos + 4] != ZSTD_MAGIC:
            pos = data.find(ZSTD_MAGIC, pos)  # skip any inter-frame padding
    return frames


def looks_like_wad(buf: bytes) -> bool:
    """Detect a League WAD v3 by its table structure (header may be zeroed)."""
    if len(buf) < 300:
        return False
    count = struct.unpack_from("<I", buf, 268)[0]
    if not (0 < count < 500_000) or 272 + count * 32 > len(buf):
        return False
    data_start = 272 + count * 32
    # every chunk's data offset must point at/after the end of the TOC
    return min(
        struct.unpack_from("<QIII", buf, 272 + i * 32)[1] for i in range(count)
    ) >= data_start


def find_wad_and_preview(frames: list[bytes]) -> tuple[bytes, bytes | None]:
    wad = next((f for f in frames if looks_like_wad(f)), None)
    if wad is None:
        raise ValueError("no WAD-structured frame found")
    preview = next((f for f in frames if f[:3] == JPEG_MAGIC), None)
    return wad, preview


# --------------------------------------------------------------------------- #
# WAD chunks
# --------------------------------------------------------------------------- #
class Chunk:
    __slots__ = ("path_hash", "raw", "csize", "usize", "ctype", "checksum")

    def __init__(self, path_hash, raw, csize, usize, ctype, checksum):
        self.path_hash = path_hash
        self.raw = raw
        self.csize = csize
        self.usize = usize
        self.ctype = ctype
        self.checksum = checksum


def read_chunks(wad: bytes) -> list[Chunk]:
    count = struct.unpack_from("<I", wad, 268)[0]
    chunks = []
    for i in range(count):
        base = 272 + i * 32
        path_hash, off, csize, usize = struct.unpack_from("<QIII", wad, base)
        ctype = wad[base + 20] & 0x0F
        checksum = struct.unpack_from("<Q", wad, base + 24)[0]
        chunks.append(Chunk(path_hash, bytes(wad[off:off + csize]),
                            csize, usize, ctype, checksum))
    return chunks


def decompress_chunk(chunk: Chunk, dctx: zstd.ZstdDecompressor) -> bytes:
    """Inflate one chunk. Types: 0 stored, 1 gzip, 3 zstd, 4 zstd subchunks."""
    if chunk.ctype == 0:
        return chunk.raw
    if chunk.ctype == 1:
        return gzip.decompress(chunk.raw)
    # 2/3 = zstd, 4 = zstd split across subchunks
    across = chunk.ctype == 4
    return dctx.decompressobj(read_across_frames=across).decompress(chunk.raw)


def validate(chunks: list[Chunk]) -> tuple[int, int, list[int]]:
    """Return (valid, total, failed_hashes); failed chunks don't match usize."""
    dctx = zstd.ZstdDecompressor()
    valid = 0
    failed: list[int] = []
    for c in chunks:
        try:
            if len(decompress_chunk(c, dctx)) == c.usize:
                valid += 1
            else:
                failed.append(c.path_hash)
        except Exception:
            failed.append(c.path_hash)
    return valid, len(chunks), failed


# --------------------------------------------------------------------------- #
# Detection: champion + locale voice banks
# --------------------------------------------------------------------------- #
_SKINS_RE = re.compile(rb"Characters/([A-Za-z0-9]+)/Skins")
_ROOTBIN_RE = re.compile(rb"Characters/([A-Za-z0-9]+)/\1\.bin")
_BANK_RE = re.compile(rb"ASSETS/Sounds/[ -~]+?\.(?:bnk|wpk)", re.IGNORECASE)
_VO_LOCALE_RE = re.compile(r"/VO/([A-Za-z]{2}_[A-Za-z]{2})/", re.IGNORECASE)
_ASSET_REF_RE = re.compile(
    rb"ASSETS/[ -~]+?\.(?:dds|tex|skn|skl|anm|bnk|wpk|scb|sco)", re.IGNORECASE)
# a particle asset that belongs to ANOTHER champion's WAD (white-box risk)
_CROSS_REF_RE = re.compile(
    rb"ASSETS/Characters/([A-Za-z0-9]+)/[ -~]+?\.(?:tex|dds|scb|sco)", re.IGNORECASE)


def _iter_decompressed(chunks, dctx):
    for c in chunks:
        try:
            yield decompress_chunk(c, dctx)
        except Exception:
            continue


def detect_champion(chunks: list[Chunk]) -> tuple[str, Counter]:
    """Majority vote over Characters/<X>/ references.

    Returns (exact_case_name, votes) so callers can report the tally.
    """
    dctx = zstd.ZstdDecompressor()
    votes: Counter[str] = Counter()
    case_for: dict[str, str] = {}
    for d in _iter_decompressed(chunks, dctx):
        if d[:4] != b"PROP":
            # still scan raw bytes (some refs live outside PROP bins)
            for m in _SKINS_RE.findall(d):
                name = m.decode()
                votes[name.lower()] += 1
                case_for.setdefault(name.lower(), name)
            continue
        for m in _ROOTBIN_RE.findall(d):
            name = m.decode()
            votes[name.lower()] += 2  # root-bin reference is a strong signal
            case_for[name.lower()] = name
        for m in _SKINS_RE.findall(d):
            name = m.decode()
            votes[name.lower()] += 1
            case_for.setdefault(name.lower(), name)
    if not votes:
        raise ValueError("could not detect champion from bins")
    champ = votes.most_common(1)[0][0]
    return case_for.get(champ, champ.capitalize()), votes


def detect_locale_banks(chunks: list[Chunk]):
    """Return (by_locale_hashes, banks) for VO banks present in this WAD.

    by_locale_hashes: locale -> set of path-hashes (used to split the wad)
    banks:            sorted list of (locale, path) for reporting
    """
    dctx = zstd.ZstdDecompressor()
    present = {c.path_hash for c in chunks}
    by_locale: dict[str, set[int]] = {}
    banks: set[tuple[str, str]] = set()
    for d in _iter_decompressed(chunks, dctx):
        if d[:4] != b"PROP":
            continue
        for raw_path in _BANK_RE.findall(d):
            path = raw_path.decode("latin1")
            m = _VO_LOCALE_RE.search(path)
            if not m:
                continue  # SFX (no locale) stays in the champion wad
            locale = m.group(1)
            # normalize casing to the canonical form if we recognize it
            canon = next((l for l in KNOWN_LOCALES if l.lower() == locale.lower()),
                         locale)
            h = xxh64(path)
            if h in present:
                by_locale.setdefault(canon, set()).add(h)
                banks.add((canon, path))
    return by_locale, sorted(banks)


_SKIN_SEG_RE = re.compile(r"/Skins/([^/]+)/", re.IGNORECASE)


def audit_references(chunks: list[Chunk]) -> tuple[dict[str, dict], str | None]:
    """Scan bins for asset references; tally bundled vs not-bundled by type.

    Returns (by_ext, primary_skin). Not-bundled references are usually shared
    base-game assets (a skin reuses base animations, shared particles, etc.) and
    are normal. The real red flag is a reference under the skin's *own* skin path
    (primary_skin) that isn't bundled -> missing texture (white box), missing
    mesh (invisible), etc. primary_skin is the Skins/<seg>/ most assets live in.
    """
    dctx = zstd.ZstdDecompressor()
    present = {c.path_hash for c in chunks}
    seen: set[str] = set()
    for d in _iter_decompressed(chunks, dctx):
        if d[:4] != b"PROP":
            continue
        for raw in _ASSET_REF_RE.findall(d):
            seen.add(raw.decode("latin1"))
    by_ext: dict[str, dict] = {}
    seg_votes: Counter[str] = Counter()
    for ref in sorted(seen):
        ext = ref.rsplit(".", 1)[-1].lower()
        slot = by_ext.setdefault(ext, {"bundled": 0, "missing": []})
        bundled = xxh64(ref) in present
        if bundled:
            slot["bundled"] += 1
            m = _SKIN_SEG_RE.search(ref)
            if m:  # only bundled assets vote for the skin's own path
                seg_votes[m.group(1)] += 1
        else:
            slot["missing"].append(ref)
    primary = seg_votes.most_common(1)[0][0] if seg_votes else None
    return by_ext, primary


def cross_champion_refs(chunks: list[Chunk], host: str):
    """Find texture/particle refs that point into ANOTHER champion's WAD.

    These are the classic white-box culprit: the bytes may even be bundled, but
    the game resolves them from <OtherChampion>.wad (not loaded unless that
    champion is in the game), so they render as blank white quads. Returns
    (refs, by_source_counter, n_bundled).
    """
    dctx = zstd.ZstdDecompressor()
    present = {c.path_hash for c in chunks}
    refs: set[str] = set()
    for d in _iter_decompressed(chunks, dctx):
        if d[:4] != b"PROP":
            continue
        for m in _CROSS_REF_RE.finditer(d):  # finditer -> need full match, not group
            champ = m.group(1).decode()
            if champ.lower() == host.lower():
                continue
            refs.add(m.group(0).decode("latin1"))
    by_source = Counter(r.split("/")[2] for r in refs)
    n_bundled = sum(1 for r in refs if xxh64(r) in present)
    return refs, by_source, n_bundled


def _localize_path(old: str, host: str) -> str:
    """Map a foreign asset path to a collision-free host-local path that
    resolves from the host champion's own WAD."""
    after = old[len("ASSETS/Characters/"):]
    return f"ASSETS/Characters/{host}/xport/" + after.replace("/", "_")


def _replace_field_strings(field, mapping, counter):
    """Recursively rewrite any string field whose value is a mapped path."""
    data = getattr(field, "data", None)
    if isinstance(data, str):
        if data in mapping:
            field.data = mapping[data]
            counter[0] += 1
    elif isinstance(data, list):
        for i, it in enumerate(data):
            if isinstance(it, str):
                if it in mapping:
                    data[i] = mapping[it]
                    counter[0] += 1
            else:
                _replace_field_strings(it, mapping, counter)
    elif isinstance(data, dict):
        for k in list(data.keys()):
            v = data[k]
            if isinstance(v, str):
                if v in mapping:
                    data[k] = mapping[v]
                    counter[0] += 1
            else:
                _replace_field_strings(v, mapping, counter)


def localize_cross_champion(chunks: list[Chunk], host: str):
    """Re-path every bundled cross-champion texture to a host-local path and
    rewrite the references that point at it, so they resolve from the host WAD.

    Needs pyRitoFile (pip install pyRitoFile) to safely re-serialize the bins,
    since changing a path's length shifts nested size fields. Returns
    (new_chunks, stats).
    """
    try:
        import tempfile as _tf
        from pyritofile import BIN
    except ImportError:
        raise RuntimeError(
            "--fix-cross-champion requires the pyRitoFile package "
            "(pip install pyRitoFile)")

    refs, by_source, _ = cross_champion_refs(chunks, host)
    present = {c.path_hash for c in chunks}
    mapping = {r: _localize_path(r, host) for r in refs if xxh64(r) in present}
    stats = {"refs": len(refs), "mapped": len(mapping), "rewritten": 0,
             "aliased": 0, "by_source": by_source}
    if not mapping:
        return chunks, stats

    cctx = zstd.ZstdCompressor(level=19)
    dctx = zstd.ZstdDecompressor()
    encoded = {k.encode("latin1") for k in mapping}
    out: list[Chunk] = []
    for c in chunks:
        try:
            d = decompress_chunk(c, dctx)
        except Exception:
            out.append(c)
            continue
        if d[:4] != b"PROP" or not any(k in d for k in encoded):
            out.append(c)
            continue
        with _tf.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
            tf.write(d)
            inp = tf.name
        b = BIN()
        b.read(inp)
        os.unlink(inp)
        cnt = [0]
        for e in b.entries:
            for f in e.data:
                _replace_field_strings(f, mapping, cnt)
        if cnt[0] == 0:
            out.append(c)
            continue
        with _tf.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
            outp = tf.name
        b.write(outp)
        nd = open(outp, "rb").read()
        os.unlink(outp)
        comp = cctx.compress(nd)
        out.append(Chunk(c.path_hash, comp, len(comp), len(nd), 3,
                        xxhash.xxh3_64(comp).intdigest()))
        stats["rewritten"] += cnt[0]

    # add the foreign textures back under their new host-local hashes
    by_hash = {c.path_hash: c for c in chunks}
    have = {c.path_hash for c in out}
    for old, new in mapping.items():
        oc = by_hash.get(xxh64(old))
        nh = xxh64(new)
        if oc and nh not in have:
            out.append(Chunk(nh, oc.raw, oc.csize, oc.usize, oc.ctype, oc.checksum))
            have.add(nh)
            stats["aliased"] += 1
    return out, stats


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def build_wad(chunks: list[Chunk]) -> bytes:
    """Reassemble a valid WAD v3.3 from a list of chunks (header restored)."""
    n = len(chunks)
    data_start = 272 + n * 32
    head = bytearray(data_start)
    body = bytearray()
    head[0:2] = b"RW"
    head[2] = 3  # major
    head[3] = 3  # minor
    struct.pack_into("<I", head, 268, n)
    for i, c in enumerate(chunks):
        base = 272 + i * 32
        struct.pack_into("<QIII", head, base, c.path_hash,
                        len(body) + data_start, c.csize, c.usize)
        head[base + 20] = c.ctype
        head[base + 21] = 0
        struct.pack_into("<Q", head, base + 24, c.checksum)
        body += c.raw
    return bytes(head) + bytes(body)


def ascii_safe(stem: str, fallback: str) -> str:
    """Strip non-ASCII (CJK filenames break the download), keep it tidy."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned or fallback


def package_fantome(out_path, wads, info, preview):
    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr("META/info.json", json.dumps(info, ensure_ascii=False, indent=2))
        if preview is not None and Image is not None:
            buf = io.BytesIO()
            try:
                Image.open(io.BytesIO(preview)).convert("RGB").save(buf, "PNG")
                z.writestr("META/image.png", buf.getvalue())
            except Exception:
                pass
        for wad_name, wad_bytes in wads.items():
            # WADs are already compressed internally; store them
            z.writestr(f"WAD/{wad_name}", wad_bytes, zipfile.ZIP_STORED)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def convert(src_path, out_dir, *, name=None, author=None,
            split_vo=True, want_preview=True, fix_cross_champion=False,
            quiet=False, verbose=False):
    def log(*a):
        if not quiet:
            print(*a)

    def vlog(*a):
        if verbose and not quiet:
            print(*a)

    data = open(src_path, "rb").read()
    if data[:8] != WGZ_MAGIC:
        raise ValueError(f"{src_path}: not a WGZSkins (.lolgezi) file")

    frames = read_frames(data)
    wad, preview = find_wad_and_preview(frames)
    vlog(f"    container: {len(data)/1e6:.1f} MB, {len(frames)} frames; "
         f"WAD frame {len(wad)/1e6:.1f} MB; "
         f"preview: {'yes' if preview else 'no'}")
    if not want_preview:
        preview = None
    chunks = read_chunks(wad)

    type_dist = Counter(c.ctype for c in chunks)
    type_names = {0: "stored", 1: "gzip", 3: "zstd", 4: "zstd-sub"}
    vlog("    chunk types: " + ", ".join(
        f"{type_names.get(t, t)}={n}" for t, n in sorted(type_dist.items())))

    valid, total, failed = validate(chunks)
    champ, votes = detect_champion(chunks)
    log(f"  champion: {champ}   chunks: {valid}/{total} valid")
    if verbose:
        top = ", ".join(f"{n}:{v}" for n, v in votes.most_common(5))
        vlog(f"    champion votes: {top}")
        if failed:
            vlog(f"    {len(failed)} chunk(s) failed validation: "
                 + ", ".join(f"{h:016x}" for h in failed[:8])
                 + (" ..." if len(failed) > 8 else ""))

    # cross-champion references (white-box risk) — detect, report, optionally fix
    xrefs, xsrc, xbundled = cross_champion_refs(chunks, champ)
    if xrefs:
        srcs = ", ".join(f"{c}:{n}" for c, n in xsrc.most_common())
        if verbose:
            vlog(f"    cross-champion refs: {len(xrefs)} "
                 f"({xbundled} bundled at FOREIGN paths -> white-box risk) "
                 f"from {srcs}")
        if fix_cross_champion:
            chunks, st = localize_cross_champion(chunks, champ)
            log(f"  fix-cross-champion: localized {st['mapped']} textures, "
                f"rewrote {st['rewritten']} refs, added {st['aliased']} chunks")
        else:
            log(f"  note: {len(xrefs)} cross-champion VFX refs "
                f"(white-box risk); re-run with --fix-cross-champion to localize")

    # split out locale voice banks
    locale_hashes: dict[str, set[int]] = {}
    banks: list[tuple[str, str]] = []
    if split_vo:
        locale_hashes, banks = detect_locale_banks(chunks)
    if verbose and banks:
        vlog("    voice banks (-> locale wad):")
        for loc, path in banks:
            vlog(f"      [{loc}] {path.rsplit('/', 1)[-1]}")

    all_locale = set().union(*locale_hashes.values()) if locale_hashes else set()
    wads: dict[str, bytes] = {}
    main_chunks = [c for c in chunks if c.path_hash not in all_locale]
    wads[f"{champ}.wad.client"] = build_wad(main_chunks)
    for locale, hashes in locale_hashes.items():
        loc_chunks = [c for c in chunks if c.path_hash in hashes]
        wads[f"{champ}.{locale}.wad.client"] = build_wad(loc_chunks)
        log(f"  locale wad: {champ}.{locale}.wad.client ({len(loc_chunks)} banks)")

    if verbose:
        audit, primary = audit_references(chunks)
        base_like = primary is None or primary.lower() in ("base", "skin0")
        vlog("    reference audit (bundled / not-bundled by type)"
             + (f"; skin's own path: Skins/{primary}/" if primary else "") + ":")
        for ext in sorted(audit):
            info_ = audit[ext]
            miss = info_["missing"]
            # a missing ref under the skin's OWN (non-base) path is the red flag;
            # missing base-game/shared refs are normal and not flagged
            suspicious = []
            if not base_like:
                for m in miss:
                    seg = _SKIN_SEG_RE.search(m)
                    if seg and seg.group(1).lower() == primary.lower():
                        suspicious.append(m)
            note = f"  <-- {len(suspicious)} under own Skins/{primary}/ (CHECK)" \
                if suspicious else ""
            vlog(f"      .{ext}: {info_['bundled']} bundled, "
                 f"{len(miss)} from base game{note}")
            for m in suspicious[:5]:
                vlog(f"          MISSING: {m}")

    stem = ascii_safe(os.path.splitext(os.path.basename(src_path))[0], champ)
    out_name = (name or stem)
    if not out_name.lower().endswith(".fantome"):
        out_name += ".fantome"
    out_path = os.path.join(out_dir, out_name)

    info = {
        "Name": name or f"{champ} ({stem})",
        "Author": author or "lolgezi/WGZSkins import",
        "Version": "1.0.0",
        "Description": f"Converted from {os.path.basename(src_path)} "
                       f"(WGZSkins). Champion={champ}.",
    }
    os.makedirs(out_dir, exist_ok=True)
    package_fantome(out_path, wads, info, preview)
    size_mb = os.path.getsize(out_path) / 1e6
    log(f"  -> {out_path}  ({size_mb:.1f} MB)")
    return out_path


def gather_inputs(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                      if f.lower().endswith(".lolgezi")]
        else:
            files.append(p)
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="overthewall",
        description="Convert WGZSkins (.lolgezi) custom skins to .fantome mods.")
    ap.add_argument("inputs", nargs="+",
                    help=".lolgezi file(s), or a folder to batch-convert")
    ap.add_argument("-o", "--output-dir", default=".",
                    help="where to write .fantome files (default: current dir)")
    ap.add_argument("--name", help="override mod name / output filename")
    ap.add_argument("--author", help="set the mod author in info.json")
    ap.add_argument("--no-split-vo", action="store_true",
                    help="keep voice banks in the champion wad (don't split locale)")
    ap.add_argument("--no-preview", action="store_true",
                    help="skip embedding the splash preview image")
    ap.add_argument("--fix-cross-champion", action="store_true",
                    help="localize particle textures borrowed from other "
                         "champions to host-local paths (fixes white-box VFX); "
                         "requires the pyRitoFile package")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print detailed diagnostics (votes, chunk types, "
                         "voice banks, and a bundled-vs-missing reference audit)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    files = gather_inputs(args.inputs)
    if not files:
        ap.error("no .lolgezi inputs found")

    ok = 0
    for f in files:
        if not args.quiet:
            print(f"[*] {os.path.basename(f)}")
        try:
            convert(f, args.output_dir, name=args.name if len(files) == 1 else None,
                    author=args.author, split_vo=not args.no_split_vo,
                    want_preview=not args.no_preview,
                    fix_cross_champion=args.fix_cross_champion,
                    quiet=args.quiet, verbose=args.verbose)
            ok += 1
        except Exception as e:
            print(f"  ! failed: {e}", file=sys.stderr)
    if not args.quiet:
        print(f"\nDone: {ok}/{len(files)} converted.")
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
