#!/usr/bin/env python3
"""Opt-in headless DiffSinger acceptance check for the timing fixture."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


FINAL_VOCAL_ENDPOINT_SECONDS = 4.0


def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        list(command),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def audio_duration(path: Path) -> float:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    report = json.loads(result.stdout)
    return float(report["format"]["duration"])


def measure_region(path: Path, start: float, duration: float) -> str:
    result = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ]
    )
    return "\n".join(
        line.strip()
        for line in result.stderr.splitlines()
        if "mean_volume:" in line or "max_volume:" in line
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--melisma", type=Path, default=Path("./build/melisma"))
    parser.add_argument(
        "--openutau-project",
        type=Path,
        default=Path("~/OpenUtau-CLI/OpenUtau.Cli/OpenUtau.Cli.csproj"),
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--singer", default="TIGER_DS_v102")
    parser.add_argument(
        "--phonemizer",
        default=(
            "OpenUtau.Core.DiffSinger."
            "DiffSingerARPAPlusEnglishPhonemizer"
        ),
    )
    parser.add_argument("--renderer", default="DIFFSINGER")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    args.melisma = args.melisma.expanduser().resolve()
    args.openutau_project = args.openutau_project.expanduser().resolve()
    args.output = args.output.expanduser().resolve()

    for program in ("dotnet", "ffmpeg", "ffprobe"):
        if shutil.which(program) is None:
            print(f"required program is unavailable: {program}", file=sys.stderr)
            return 1
    if args.output.exists() and any(args.output.iterdir()):
        print(f"refusing non-empty output directory: {args.output}", file=sys.stderr)
        return 1
    args.output.mkdir(parents=True, exist_ok=True)

    bundle = args.output / "melisma"
    vocal = args.output / "vocal.wav"
    run(
        [
            str(args.melisma),
            "render-inputs",
            "--singer",
            args.singer,
            "--phonemizer",
            args.phonemizer,
            "--renderer",
            args.renderer,
            str(root / "examples" / "intro-vocal-interlude.mid"),
            str(root / "examples" / "intro-vocal-interlude.txt"),
            str(bundle),
        ]
    )
    run(
        [
            "dotnet",
            "run",
            "--project",
            str(args.openutau_project),
            "--no-build",
            "--",
            "render",
            str(bundle / "vocal.ustx"),
            "--out",
            str(vocal),
        ]
    )
    if not vocal.is_file() or vocal.stat().st_size == 0:
        print(f"vocal WAV is absent or empty: {vocal}", file=sys.stderr)
        return 1

    duration = audio_duration(vocal)
    if duration <= FINAL_VOCAL_ENDPOINT_SECONDS:
        print(
            f"vocal duration {duration:.3f}s does not extend beyond "
            f"{FINAL_VOCAL_ENDPOINT_SECONDS:.3f}s",
            file=sys.stderr,
        )
        return 1

    print(f"WAV duration: {duration:.3f}s")
    print("Leading region 0.0-2.0s:")
    print(measure_region(vocal, 0.0, 2.0) or "(no volumedetect summary)")
    print("Vocal region 2.0-4.0s:")
    print(measure_region(vocal, 2.0, 2.0) or "(no volumedetect summary)")
    print("No permanent RMS threshold is applied; compare the measured regions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
