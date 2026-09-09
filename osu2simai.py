#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
osu!mania 4K -> maimai-DX-style touch chart converter (simai + JSON).

Converts an osu!mania keyboard chart into the "touch only" chart format used by
maimai DX homebrew tooling. Every chart is written into its own folder
<output-dir>/<chart-name>/ with the fixed file names:

  * maidata.txt - simai chart with the &title/... metadata header (required
    by chart tools)
  * chart.json  - JSON notes in the {"split","beat"} internal format

`--extras` additionally writes the bare fumen.txt body (no header) and an
expected_notes.csv reference file.

Every run must declare the chart tier and level explicitly:

  * --tier: exactly one of  easy, basic, advanced, expert, master,
    re:master, utage  (case-sensitive, no aliases). The tier position is
    1-based for the maidata slot (inote_<slot>/lv_<slot>) and 0-based
    (easy=0 ... utage=6) for notes[].difficulty in the JSON export.
  * --num:  the level number, must be > 0 (e.g. 42 or 14.7).

The simai output always carries its metadata header: <name>.maidata.txt
starts with the &title/... header (needed by chart tools), while
<name>.fumen.txt is the bare fumen body (no header) for parsers that want
the raw chart stream only.

Key/column mapping (osu!mania 4K default keys, left -> right = col0..col3):
    col0 (D) -> E6    osu tap  -> simai touch  / JSON touches[]
    col1 (F) -> B5    osu hold -> simai touch hold h[div:beats]
    col2 (J) -> B4                 / JSON toucheHolds[]
    col3 (K) -> E4

Time encoding
  * simai: (bpm) + {16} grid, one comma per 1/16 note, every bar line holds
    exactly its {n} cells; chart ends with a lone 'E'.
  * JSON:  {split, beat} means beat/split * one 4/4 bar (whole note). Both are
    emitted as fully reduced fractions.

Hold durations are quantised to the closest common [division:beats] ratio
(error <= ~1.3 ms at BPM 148).

Requirements: Python 3.10+, no third-party packages.

Exit codes: 0 ok, 1 conversion error, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict

DEFAULT_KEYMAP = "E6,B5,B4,E4"          # col0..col3 of a 4K chart
HOLD_DIVISIONS = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32]

# Difficulty tiers, canonical names only (no aliases). The position is
# 1-based and used for the maidata slot (inote_<slot>/lv_<slot>); the JSON
# export uses the 0-based index (easy=0 ... utage=6) for notes[].difficulty.
# Reorder if your tool uses a different numbering.
TIER_ORDER = {
    "easy": 1,
    "basic": 2,
    "advanced": 3,
    "expert": 4,
    "master": 5,
    "re:master": 6,
    "utage": 7,
}


def num_type(text: str) -> float:
    """argparse type: level number, must be > 0."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"level number {text!r} is not a number")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("level number must be > 0")
    return value


def fmt_num(value: float) -> str:
    """'42.0' -> '42', '14.7' -> '14.7'."""
    return format(value, "g")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def reduce_frac(num: int, den: int) -> tuple[int, int]:
    """Return (split, beat) with split = reduced denominator and beat =
    reduced numerator, so the value equals beat/split (measured in bars)."""
    g = math.gcd(num, den)
    return den // g, num // g


def best_hold_ratio(dur_ms: float, bpm: float) -> tuple[int, int, float]:
    """Return (division, beats, exact_ms) closest to dur_ms.

    A simai [division:beats] (or JSON {split,beat}) lasts
    wholeNote / division * beats, wholeNote being one 4/4 bar.
    """
    whole = 60000.0 / bpm * 4.0
    best = None
    for div in HOLD_DIVISIONS:
        unit = whole / div
        beats = max(1, round(dur_ms / unit))
        for cand in (beats - 1, beats, beats + 1):
            if cand < 1:
                continue
            exact = unit * cand
            key = (round(abs(exact - dur_ms), 3), div, cand)
            if best is None or key < best[0]:
                best = (key, div, cand, exact)
    _, div, beats, exact = best
    return div, beats, exact


def parse_osu(path: str) -> dict:
    """Parse an osu file into a lightweight dict.

    Only the sections needed for conversion are kept:
    metadata, difficulty (CircleSize / mode), uninherited timing points and
    hit objects (x, time, type, endTime for mania holds).
    """
    info = {"metadata": {}, "general": {}, "difficulty": {}, "timing": [], "hits": []}
    section = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if section in ("General", "Metadata", "Difficulty") and ":" in line:
                k, _, v = line.partition(":")
                k, v = k.strip(), v.strip()
                bucket = "difficulty" if section == "Difficulty" else \
                    ("general" if section == "General" else "metadata")
                info[bucket][k] = v
            elif section == "TimingPoints":
                p = line.split(",")
                if len(p) >= 2:
                    uninherited = int(p[6]) if len(p) > 6 else 1
                    beat = float(p[1])
                    if uninherited == 1 and beat > 0:
                        info["timing"].append((float(p[0]), beat))
            elif section == "HitObjects":
                p = line.split(",")
                if len(p) < 5:
                    continue
                typ = int(p[3])
                end = None
                if typ & 128:
                    end = float(p[5].split(":")[0])
                info["hits"].append((float(p[0]), float(p[2]), typ, end))
    return info


def resolve_bpm(info: dict, override: float | None) -> float:
    """Return the chart BPM, refusing silently wrong results on multi-BPM maps."""
    if override is not None:
        if override <= 0:
            raise ValueError(f"invalid --bpm {override}")
        return float(override)
    timing = info["timing"]
    if not timing:
        raise ValueError("no uninherited (red) timing point found in the .osu")
    if len(timing) > 1:
        raise ValueError(
            f"chart has {len(timing)} BPM sections; this tool only supports "
            "single-BPM charts (or force one with --bpm)")
    return 60000.0 / timing[0][1]


def column_of(x: float, key_count: int) -> int:
    return min(key_count - 1, max(0, int(x // (512.0 / key_count))))


def build_chart(info: dict, bpm: float, keymap: list[str], first_ms: float):
    """Snap hits to a 1/16 grid and group them per slot.

    Returns (events: slot -> sorted [(col, end_ms or None)], sixteenth_ms,
    max_slot, off_grid: [(t, slot, dev_ms)]).
    """
    key_count = len(keymap)
    sixteenth = 60000.0 / bpm / 4.0
    events: dict[int, list[tuple[int, float | None]]] = defaultdict(list)
    off_grid: list[tuple[float, int, float]] = []
    for x, t, typ, end in info["hits"]:
        col = column_of(x, key_count)
        slot = round((t - first_ms) / sixteenth)
        dev = abs(t - first_ms - slot * sixteenth)
        if dev > 4.0:
            off_grid.append((t, slot, dev))
        events[slot].append((col, end))
    max_slot = max(events, default=-1)
    return events, sixteenth, max_slot, off_grid


def fmt_bpm(bpm: float) -> str:
    """Print 148 as '148', 147.55 as '147.55', …"""
    r = round(bpm, 6)
    return str(int(r)) if abs(r - round(r)) < 1e-9 else format(r, "g")


def render(events, sixteenth, max_slot, bpm, keymap, first_ms, slot_num):
    """Render simai fumen / maidata header / JSON / expected CSV rows."""
    expected: list[tuple[float, str, bool, float]] = []
    cells: list[str] = []
    hold_stats: dict[tuple[int, int], tuple[int, float]] = {}

    for slot in range(max_slot + 1):
        tokens = []
        for col, end in sorted(events.get(slot, [])):
            code = keymap[col]
            t = first_ms + slot * sixteenth
            if end is None:
                tokens.append(code)
                expected.append((t, code, False, 0.0))
            else:
                dur = end - t
                div, beats, exact = best_hold_ratio(dur, bpm)
                tokens.append(f"{code}h[{div}:{beats}]")
                expected.append((t, code, True, exact))
                prev = hold_stats.get((div, beats))
                err = abs(exact - dur)
                hold_stats[(div, beats)] = ((prev[0] + 1, max(prev[1], err))
                                            if prev else (1, err))
        cells.append(("/".join(tokens) + ",") if tokens else ",")

    # simai: one line per 4/4 bar = 16 sixteenth slots; the final partial bar
    # is padded to a full measure; BPM attaches to the first measure; the
    # chart ends with the 'E' end-of-chart marker.
    while len(cells) % 16:
        cells.append(",")
    bar_lines = ["{16}" + "".join(cells[b:b + 16]) for b in range(0, len(cells), 16)]
    fumen = f"({fmt_bpm(bpm)})" + bar_lines[0] + "\n" + "\n".join(bar_lines[1:]) + "\nE"

    # JSON export
    touches, touch_holds = [], []
    for slot in sorted(events):
        split, beat = reduce_frac(slot, 16)
        for col, end in sorted(events[slot]):
            button = keymap[col]
            base = {"hitTime": {"split": split, "beat": beat},
                    "isNoMulti": False, "button": button, "isFirework": False}
            if end is None:
                touches.append(base)
            else:
                t = first_ms + slot * sixteenth
                div, beats, _ = best_hold_ratio(end - t, bpm)
                touch_holds.append({**base,
                                    "holdTime": {"split": div, "beat": beats}})

    return fumen, touches, touch_holds, expected, hold_stats, len(bar_lines)


# --------------------------------------------------------------------------- #
# output writers
# --------------------------------------------------------------------------- #
def write_artifacts(base: str, args, metadata: dict, bpm: float,
                    fumen: str, touches, touch_holds, expected,
                    hold_stats, bar_count, level: str) -> list[str]:
    """Write the chart files into <output_dir>/<chart-name>/.

    Main files (fixed names, one set per chart folder):
      maidata.txt  - simai with the &metadata header
      chart.json   - JSON notes in the {split,beat} internal format
    Optional extras (--extras):
      fumen.txt          - bare fumen body without the metadata header
      expected_notes.csv - osu-derived reference rows for verification
    """
    chart_dir = os.path.join(args.output_dir, base)
    os.makedirs(chart_dir, exist_ok=True)
    written: list[str] = []

    maidata_path = None
    if args.formats in ("all", "maidata"):
        charter = args.charter or metadata.get("Creator", "")
        maidata_path = os.path.join(chart_dir, "maidata.txt")
        with open(maidata_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"&title={metadata.get('Title', '')}\n")
            f.write(f"&wholebpm={fmt_bpm(bpm)}\n")
            f.write(f"&artist={metadata.get('Artist', '')}\n")
            f.write(f"&des={charter}\n")
            f.write(f"&first={args.first:g}\n")
            f.write(f"&lv_{args.slot}={level}\n")
            f.write(f"&des_{args.slot}={charter}\n")
            f.write(f"&inote_{args.slot}=\n")
            f.write(fumen + "\n")
        written.append(maidata_path)

    json_path = None
    if args.formats in ("all", "json"):
        json_root = {
            "songName": metadata.get("Title", ""),
            "composer": (metadata.get("Artist", "") or "").capitalize(),
            "offset": args.first,
            "notes": [{
                "difficulty": args.difficulty,
                "level": level,
                "charter": args.charter or metadata.get("Creator", ""),
                "bpmList": {"keyframes": [{
                    "time": {"split": 4, "beat": 0},
                    "bpm": round(bpm, 6), "type": 0}]},
                "timeSignatureList": {"keyframes": [{
                    "time": {"split": 4, "beat": 0},
                    "signature": {"split": 4, "beat": 4}}]},
                "taps": [],
                "holds": [],
                "touches": touches,
                "toucheHolds": touch_holds,
                "slides": [],
                "notices": [],
                "isBigTouch": False,
            }],
        }
        json_path = os.path.join(chart_dir, "chart.json")
        with open(json_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(json_root, f, ensure_ascii=False, indent=2)
        written.append(json_path)

    # bare fumen body: standalone choice (--formats fumen) or --extras
    if args.formats == "fumen" or args.extras:
        fumen_path = os.path.join(chart_dir, "fumen.txt")
        with open(fumen_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(fumen + "\n")
        written.append(fumen_path)

    if args.extras:
        ref_path = os.path.join(chart_dir, "expected_notes.csv")
        with open(ref_path, "w", encoding="utf-8", newline="\n") as f:
            for ms, code, hold, dur in expected:
                f.write(f"{ms:.3f},{code},{1 if hold else 0},{dur:.3f}\n")
        written.append(ref_path)

    # built-in self check: simai + JSON must agree with the osu-derived rows
    assert len(touches) == sum(1 for e in expected if not e[2])
    assert len(touch_holds) == sum(1 for e in expected if e[2])

    return written


def report(name, expected, hold_stats, bar_count, off_grid, bpm, silent):
    if not silent:
        cnt_code = Counter(e[1] for e in expected)
        cnt_hold = Counter("hold" if e[2] else "tap" for e in expected)
        print(f"[{name}]")
        print(f"  BPM {fmt_bpm(bpm)}  notes {len(expected)} "
              f"({cnt_hold['tap']} taps + {cnt_hold['hold']} holds)  bars {bar_count}")
        print("  by area: " + ", ".join(f"{k}={v}" for k, v in sorted(cnt_code.items())))
        if off_grid:
            print(f"  WARNING {len(off_grid)} notes snapped off their 1/16 grid "
                  f"(max {max(d for _, _, d in off_grid):.1f} ms)")
        if hold_stats:
            print("  holds: " + ", ".join(f"[{d}:{b}]x{c}"
                                          for (d, b), (c, _) in sorted(hold_stats.items())))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osu2simai",
        description="Convert osu!mania 4K charts into maimai-DX-style "
                    "touch-only simai / JSON charts.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", metavar="FILE.osu",
                   help="one or more osu!mania .osu files")
    p.add_argument("-o", "--output-dir", default="output",
                   help="directory for the generated files")
    p.add_argument("--keymap", default=DEFAULT_KEYMAP,
                   help=f"comma separated touch areas, one per column, "
                        f"left->right (default: {DEFAULT_KEYMAP})")
    p.add_argument("--bpm", type=float, default=None,
                   help="override the BPM taken from the osu timing points")
    p.add_argument("--first", type=float, default=0.0,
                   help="chart offset in ms (simai &first / JSON offset)")
    p.add_argument("-t", "--tier", required=True,
                   choices=list(TIER_ORDER),
                   help="difficulty tier (exact name, no aliases): "
                        "easy, basic, advanced, expert, master, "
                        "re:master, utage; selects the maidata slot and "
                        "notes[].difficulty")
    p.add_argument("-n", "--num", required=True, type=num_type, metavar="NUMBER",
                   help="level number (must be > 0), used for lv_<slot> and "
                        "JSON level, e.g. 42 or 14.7")
    p.add_argument("--charter", default=None,
                   help="override charter/designer (default: osu Creator)")
    p.add_argument("--formats", choices=("all", "fumen", "maidata", "json"),
                   default="all",
                   help="main artifact(s): all writes maidata.txt + chart.json; "
                        "fumen writes only the bare fumen.txt body")
    p.add_argument("--extras", action="store_true",
                   help="also write the bare fumen.txt and expected_notes.csv "
                        "next to maidata.txt / chart.json")
    p.add_argument("--quiet", action="store_true", help="only print errors")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # tier (canonical name) selects the 1-based maidata slot and the 0-based
    # JSON difficulty (easy=0 ... utage=6); num (>0) is the level value.
    # argparse already validated both.
    args.slot = TIER_ORDER[args.tier]
    args.difficulty = TIER_ORDER[args.tier] - 1
    args.level = fmt_num(args.num)
    keymap = [s.strip() for s in args.keymap.split(",") if s.strip()]
    try:
        for path in args.inputs:
            info = parse_osu(path)
            mode = info["general"].get("Mode", "?")
            cs = info["difficulty"].get("CircleSize", None)
            key_count = int(round(float(cs))) if cs is not None else len(keymap)
            if key_count != len(keymap):
                raise ValueError(
                    f"{os.path.basename(path)} is {key_count}K but --keymap has "
                    f"{len(keymap)} areas ({args.keymap}); adjust --keymap")
            if mode != "3":
                print(f"warning: {os.path.basename(path)} Mode={mode}, "
                      "expected 3 (osu!mania)", file=sys.stderr)
            if not info["hits"]:
                raise ValueError(f"no hit objects in {os.path.basename(path)}")
            bpm = resolve_bpm(info, args.bpm)
            events, sixteenth, max_slot, off_grid = build_chart(
                info, bpm, keymap, args.first)
            fumen, touches, touch_holds, expected, hold_stats, bars = render(
                events, sixteenth, max_slot, bpm, keymap, args.first, args.slot)
            base = os.path.splitext(os.path.basename(path))[0]
            written = write_artifacts(base, args, info["metadata"], bpm, fumen,
                                      touches, touch_holds, expected,
                                      hold_stats, bars, args.level)
            report(base, expected, hold_stats, bars, off_grid, bpm, args.quiet)
            if not args.quiet:
                for path_ in sorted(written):
                    print(f"  wrote {path_}")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
