#!/usr/bin/env python3
"""Orchestrate the prototype Melisma vocal and YuE2 accompaniment pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence


DEFAULT_MELISMA = "./build/melisma"
DEFAULT_OPENUTAU_PROJECT = "~/OpenUtau-CLI/OpenUtau.Cli/OpenUtau.Cli.csproj"
DEFAULT_YUE2_REPOSITORY = "/workspace/YuE"
DEFAULT_VOCAL_GAIN = 0.5
DEFAULT_ACCOMPANIMENT_GAIN = 1.0
DEFAULT_MASTER_GAIN = 0.707945784
MIN_ATEMPO_RATIO = 0.5
MAX_ATEMPO_RATIO = 2.0
CONFORMED_DURATION_TOLERANCE_SECONDS = 0.001

STAGE_NAMES = (
    "preflight",
    "melisma-render-inputs",
    "openutau-vocal-render",
    "create-remote-run",
    "upload-accompaniment-abc",
    "inspect-uploaded-abc",
    "prepare-yue2",
    "inspect-prepared-score",
    "generate-yue2",
    "verify-yue2-generation",
    "locate-generated-audio",
    "download-full-accompaniment",
    "extract-accompaniment",
    "conform-accompaniment",
    "mix-final",
    "validate-final-mix",
)


class PipelineError(RuntimeError):
    """A pipeline failure with optional external-command diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        exit_status: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_run_id(directory_name: str, seed: int) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", directory_name).strip(".-_")
    if not safe_name:
        safe_name = "melisma"
    fingerprint = hashlib.sha256(directory_name.encode("utf-8")).hexdigest()[:8]
    return f"{safe_name[:80]}-{seed}-{fingerprint}"


def ensure_output_directory(path: Path, resume: bool = False) -> None:
    if path.exists():
        if not path.is_dir():
            raise PipelineError(f"output path exists and is not a directory: {path}")
        if any(path.iterdir()) and not resume:
            raise PipelineError(f"refusing to overwrite non-empty output directory: {path}")
    else:
        path.mkdir(parents=True)


def require_nonempty_file(path: Path, description: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise PipelineError(f"{description} is absent or empty: {path}")


def ssh_base_args(args: argparse.Namespace) -> list[str]:
    return [
        "ssh",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={args.ssh_connect_timeout}",
        "-i",
        str(args.ssh_key),
        "-p",
        str(args.runpod_port),
        f"{args.runpod_user}@{args.runpod_host}",
    ]


def scp_base_args(args: argparse.Namespace) -> list[str]:
    return [
        "scp",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(args.ssh_key),
        "-P",
        str(args.runpod_port),
    ]


def remote_command(repository: str, arguments: Sequence[str]) -> str:
    return f"cd {shlex.quote(repository)} && {shlex.join(arguments)}"


def create_remote_run_command(remote_run: str, resume: bool) -> list[str]:
    command = ["mkdir"]
    if resume:
        command.append("-p")
    command.extend(["--", remote_run])
    return command


def melisma_command(args: argparse.Namespace, melisma_output: Path) -> list[str]:
    return [
        str(args.melisma),
        "render-inputs",
        "--singer",
        args.singer,
        "--phonemizer",
        args.phonemizer,
        "--renderer",
        args.renderer,
        str(args.midi),
        str(args.lyrics),
        str(melisma_output),
    ]


def openutau_command(args: argparse.Namespace, ustx: Path, vocal: Path) -> list[str]:
    return [
        "dotnet",
        "run",
        "--project",
        str(args.openutau_project),
        "--no-build",
        "--",
        "render",
        str(ustx),
        "--out",
        str(vocal),
    ]


def extraction_command(full_audio: Path, output: Path, duration: float) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(full_audio),
        "-t",
        format(duration, ".15g"),
        "-c:a",
        "pcm_s16le",
        str(output),
    ]


def manual_conformance_anchors(path: Path) -> list[tuple[float, float]]:
    try:
        raw_anchors = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineError(
            f"could not read accompaniment conformance anchors: {path}"
        ) from error
    if not isinstance(raw_anchors, list):
        raise PipelineError("accompaniment conformance anchors must be a JSON array")

    anchors: list[tuple[float, float]] = []
    for index, raw_anchor in enumerate(raw_anchors):
        if not isinstance(raw_anchor, dict):
            raise PipelineError(f"conformance anchor {index} must be a JSON object")
        try:
            source = float(raw_anchor["source_seconds"])
            destination = float(raw_anchor["destination_seconds"])
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineError(
                f"conformance anchor {index} must contain numeric "
                "source_seconds and destination_seconds"
            ) from error
        anchors.append((source, destination))
    return anchors


def atempo_ratio(source_duration: float, destination_duration: float) -> float:
    if (
        not math.isfinite(source_duration)
        or not math.isfinite(destination_duration)
        or source_duration <= 0
        or destination_duration <= 0
    ):
        raise PipelineError("conformance segment durations must be positive and finite")
    ratio = source_duration / destination_duration
    if not MIN_ATEMPO_RATIO <= ratio <= MAX_ATEMPO_RATIO:
        raise PipelineError(
            f"conformance atempo ratio {ratio:.9g} is outside the supported "
            f"range {MIN_ATEMPO_RATIO:g}..{MAX_ATEMPO_RATIO:g}"
        )
    return ratio


def conformance_segments(
    anchors: Sequence[tuple[float, float]],
    nominal_duration: float,
) -> list[dict[str, float]]:
    if not math.isfinite(nominal_duration) or nominal_duration <= 0:
        raise PipelineError("nominal score duration must be positive and finite")
    if len(anchors) < 2:
        raise PipelineError("at least two accompaniment conformance anchors are required")

    normalized: list[tuple[float, float]] = []
    for index, anchor in enumerate(anchors):
        if len(anchor) != 2:
            raise PipelineError(f"conformance anchor {index} must contain two times")
        source = float(anchor[0])
        destination = float(anchor[1])
        if (
            not math.isfinite(source)
            or not math.isfinite(destination)
            or source < 0
            or destination < 0
        ):
            raise PipelineError(
                f"conformance anchor {index} times must be finite and nonnegative"
            )
        normalized.append((source, destination))

    first_source, first_destination = normalized[0]
    if first_source != 0 or first_destination != 0:
        raise PipelineError("conformance anchors must begin at 0 -> 0")
    last_source, last_destination = normalized[-1]
    if not math.isclose(last_source, nominal_duration, abs_tol=1e-9):
        raise PipelineError(
            "final conformance source anchor must equal the nominal score duration"
        )
    if not math.isclose(last_destination, nominal_duration, abs_tol=1e-9):
        raise PipelineError(
            "final conformance destination anchor must equal the nominal score duration"
        )

    segments: list[dict[str, float]] = []
    for index, ((source_start, destination_start), (source_end, destination_end)) in enumerate(
        zip(normalized, normalized[1:])
    ):
        source_duration = source_end - source_start
        destination_duration = destination_end - destination_start
        if source_duration <= 0:
            raise PipelineError(
                f"conformance source times must increase at segment {index}"
            )
        if destination_duration <= 0:
            raise PipelineError(
                f"conformance destination times must increase at segment {index}"
            )
        ratio = atempo_ratio(source_duration, destination_duration)
        segments.append(
            {
                "source_start_seconds": source_start,
                "source_end_seconds": source_end,
                "destination_start_seconds": destination_start,
                "destination_end_seconds": destination_end,
                "source_duration_seconds": source_duration,
                "destination_duration_seconds": destination_duration,
                "stretch_ratio": destination_duration / source_duration,
                "atempo_ratio": ratio,
            }
        )
    return segments


def conformance_filter_graph(
    segments: Sequence[dict[str, float]],
    nominal_duration: float,
) -> str:
    if not segments:
        raise PipelineError("cannot build conformance filter without segments")
    filters = []
    labels = []
    for index, segment in enumerate(segments):
        label = f"segment{index}"
        labels.append(f"[{label}]")
        filters.append(
            "[0:a]"
            f"atrim=start={segment['source_start_seconds']:.15g}:"
            f"end={segment['source_end_seconds']:.15g},"
            "asetpts=PTS-STARTPTS,"
            f"atempo={segment['atempo_ratio']:.15g}"
            f"[{label}]"
        )
    filters.append(
        "".join(labels)
        + f"concat=n={len(segments)}:v=0:a=1[concatenated]"
    )
    filters.append(
        "[concatenated]"
        f"apad=whole_dur={nominal_duration:.15g},"
        f"atrim=start=0:end={nominal_duration:.15g},"
        "asetpts=PTS-STARTPTS[out]"
    )
    return ";".join(filters)


def conformance_command(
    source: Path,
    output: Path,
    segments: Sequence[dict[str, float]],
    nominal_duration: float,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        conformance_filter_graph(segments, nominal_duration),
        "-map",
        "[out]",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]


def duration_probe_command(path: Path) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]


def parse_audio_duration(output: str) -> float:
    try:
        probe = json.loads(output)
        duration = float(probe["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise PipelineError("ffprobe output lacks a valid audio duration") from error
    if not math.isfinite(duration) or duration <= 0:
        raise PipelineError("audio duration must be positive and finite")
    return duration


def accompaniment_for_mix(
    source: Path,
    conformed: Path,
    *,
    enabled: bool,
    succeeded: bool,
) -> Path:
    if not enabled:
        return source
    if not succeeded:
        raise PipelineError("enabled accompaniment conformance did not succeed")
    return conformed


def nominal_duration(inspection_output: str) -> float:
    try:
        inspection = json.loads(inspection_output)
        duration = float(inspection["nominal_duration_seconds"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise PipelineError(
            "ABC inspection lacks a valid nominal_duration_seconds"
        ) from error
    if duration <= 0:
        raise PipelineError("nominal_duration_seconds must be positive")
    return duration


def finite_positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def prepared_score_path(prepared_directory: str, prepare_output: str = "") -> str:
    if prepare_output.strip():
        try:
            report = json.loads(prepare_output.strip().splitlines()[-1])
            reported_directory = report["prepared"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise PipelineError("YuE2 preparation did not report its prepared directory") from error
        if reported_directory != prepared_directory:
            raise PipelineError(
                "YuE2 preparation reported an unexpected prepared directory: "
                f"{reported_directory}"
            )
    return str(PurePosixPath(prepared_directory) / "score.abc")


def mix_command(
    vocal: Path,
    accompaniment: Path,
    output: Path,
    vocal_gain: float,
    accompaniment_gain: float,
    master_gain: float,
) -> list[str]:
    filter_graph = (
        f"[0:a]volume={accompaniment_gain!r}[accompaniment];"
        f"[1:a]volume={vocal_gain!r}[vocal];"
        "[accompaniment][vocal]amix=inputs=2:duration=first:normalize=0[mix];"
        f"[mix]volume={master_gain!r}[out]"
    )
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(accompaniment),
        "-i",
        str(vocal),
        "-filter_complex",
        filter_graph,
        "-map",
        "[out]",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]


def volumedetect_command(path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]


def parse_volumedetect(output: str) -> dict[str, float | str]:
    analysis: dict[str, float | str] = {}
    for field in ("mean_volume", "max_volume"):
        match = re.search(
            rf"(?:^|\s){field}:\s*(-inf|[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+dB(?:\s|$)",
            output,
        )
        if not match:
            raise PipelineError(f"FFmpeg volumedetect output is missing {field}")
        value = match.group(1)
        analysis[f"{field}_db"] = "-inf" if value == "-inf" else float(value)
    return analysis


def validate_final_mix_analysis(analysis: dict[str, Any]) -> None:
    maximum = analysis.get("max_volume_db")
    if maximum == "-inf":
        return
    if not isinstance(maximum, (int, float)) or not math.isfinite(maximum):
        raise PipelineError("final mix analysis has an invalid max_volume_db")
    if maximum >= 0.0:
        raise PipelineError(
            f"final mix reached {maximum:.1f} dBFS and may be clipped"
        )


class Pipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output = args.output
        self.manifest_path = self.output / "manifest.json"
        self.current_stage = "initialization"
        self.conformance_downstream_resume = False
        if args.resume:
            self.manifest = self.load_manifest()
            self.prepare_resumed_manifest()
        else:
            self.manifest = self.new_manifest()

    def new_manifest(self) -> dict[str, Any]:
        anchors_path = self.args.accompaniment_conformance_anchors
        anchors_sha256 = None
        if anchors_path is not None:
            require_nonempty_file(anchors_path, "accompaniment conformance anchors")
            anchors_sha256 = sha256_file(anchors_path)
        return {
            "status": "running",
            "inputs": {
                "midi": str(self.args.midi),
                "lyrics": str(self.args.lyrics),
                "midi_sha256": None,
                "lyrics_sha256": None,
                "conformance_anchors_sha256": anchors_sha256,
            },
            "configuration": {
                "singer": self.args.singer,
                "phonemizer": self.args.phonemizer,
                "renderer": self.args.renderer,
                "style": self.args.style,
                "seed": self.args.seed,
                "vocal_gain": self.args.vocal_gain,
                "accompaniment_gain": self.args.accompaniment_gain,
                "master_gain": self.args.master_gain,
                "accompaniment_conformance_enabled": (
                    self.args.accompaniment_conformance_anchors is not None
                ),
                "accompaniment_conformance_anchors": (
                    str(self.args.accompaniment_conformance_anchors)
                    if self.args.accompaniment_conformance_anchors is not None
                    else None
                ),
                "accompaniment_conformance_anchors_sha256": anchors_sha256,
                "melisma": str(self.args.melisma),
                "openutau_project": str(self.args.openutau_project),
                "remote_yue2_repository": self.args.yue2_repository,
                "remote_host": self.args.runpod_host,
                "remote_port": self.args.runpod_port,
            },
            "accompaniment_conformance": {
                "enabled": self.args.accompaniment_conformance_anchors is not None,
                "succeeded": False,
                "source": None,
                "conformed": None,
                "anchors": [],
                "segments": [],
                "conformed_duration_seconds": None,
                "nominal_score_duration_seconds": None,
            },
            "artifacts": {},
            "stages": {name: {"status": "pending"} for name in STAGE_NAMES},
        }

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            raise PipelineError(
                f"--resume requires a usable manifest: {self.manifest_path}"
            )
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PipelineError(
                f"--resume could not read a valid manifest: {self.manifest_path}"
            ) from error
        if not isinstance(manifest, dict):
            raise PipelineError("--resume manifest root must be a JSON object")
        if not isinstance(manifest.get("stages"), dict):
            raise PipelineError("--resume manifest has no usable stage statuses")
        return manifest

    def prepare_resumed_manifest(self) -> None:
        expected_inputs = {
            "midi": str(self.args.midi),
            "lyrics": str(self.args.lyrics),
        }
        expected_configuration = {
            "singer": self.args.singer,
            "phonemizer": self.args.phonemizer,
            "renderer": self.args.renderer,
            "style": self.args.style,
            "seed": self.args.seed,
            "vocal_gain": self.args.vocal_gain,
            "accompaniment_gain": self.args.accompaniment_gain,
            "master_gain": self.args.master_gain,
            "melisma": str(self.args.melisma),
            "openutau_project": str(self.args.openutau_project),
            "remote_yue2_repository": self.args.yue2_repository,
        }
        inputs = self.manifest.get("inputs", {})
        configuration = self.manifest.get("configuration", {})
        for name, expected in expected_inputs.items():
            if inputs.get(name) != expected:
                raise PipelineError(
                    f"--resume input {name} does not match the existing manifest"
                )
        for name, expected in expected_configuration.items():
            if configuration.get(name) != expected:
                raise PipelineError(
                    f"--resume configuration {name} does not match the existing manifest"
                )
        configuration["remote_host"] = self.args.runpod_host
        configuration["remote_port"] = self.args.runpod_port
        stages = self.manifest["stages"]
        stages.setdefault("preflight", {"status": "success"})
        self.prepare_resumed_conformance(configuration, inputs, stages)
        incomplete = False
        for name in STAGE_NAMES:
            state = stages.setdefault(name, {"status": "pending"})
            if incomplete or state.get("status") != "success":
                incomplete = True
                if state.get("status") == "running":
                    stages[name] = {"status": "interrupted"}
                elif state.get("status") == "success":
                    stages[name] = {"status": "pending"}
        self.manifest["status"] = "running"
        self.manifest.pop("failed_stage", None)
        self.manifest.pop("error", None)

    def prepare_resumed_conformance(
        self,
        configuration: dict[str, Any],
        inputs: dict[str, Any],
        stages: dict[str, Any],
    ) -> None:
        anchors_path = self.args.accompaniment_conformance_anchors
        requested_enabled = anchors_path is not None
        requested_path = str(anchors_path) if anchors_path is not None else None
        requested_sha256 = None
        if anchors_path is not None:
            require_nonempty_file(anchors_path, "accompaniment conformance anchors")
            requested_sha256 = sha256_file(anchors_path)

        existing_path = configuration.get("accompaniment_conformance_anchors")
        conformance_state = self.manifest.get("accompaniment_conformance")
        state_enabled = (
            conformance_state.get("enabled")
            if isinstance(conformance_state, dict)
            else False
        )
        existing_enabled = configuration.get(
            "accompaniment_conformance_enabled",
            bool(existing_path) or state_enabled is True,
        )
        if not isinstance(existing_enabled, bool):
            raise PipelineError(
                "--resume manifest has invalid accompaniment conformance state"
            )
        existing_sha256 = configuration.get(
            "accompaniment_conformance_anchors_sha256"
        )
        if existing_sha256 is None:
            existing_sha256 = inputs.get("conformance_anchors_sha256")

        if existing_enabled and not requested_enabled:
            raise PipelineError(
                "--resume cannot disable previously enabled accompaniment conformance"
            )
        if existing_enabled:
            if existing_path != requested_path:
                raise PipelineError(
                    "--resume accompaniment conformance anchor path does not match "
                    "the existing manifest"
                )
            if existing_sha256 != requested_sha256:
                raise PipelineError(
                    "--resume accompaniment conformance anchor contents do not match "
                    "the existing manifest"
                )
            upstream_stages = STAGE_NAMES[: STAGE_NAMES.index("conform-accompaniment")]
            if all(
                stages.get(name, {}).get("status") == "success"
                for name in upstream_stages
            ):
                self.conformance_downstream_resume = True
        elif requested_enabled:
            upstream_stages = STAGE_NAMES[: STAGE_NAMES.index("conform-accompaniment")]
            incomplete_upstream = [
                name
                for name in upstream_stages
                if stages.get(name, {}).get("status") != "success"
            ]
            if incomplete_upstream:
                raise PipelineError(
                    "--resume cannot enable accompaniment conformance because "
                    "upstream stages are incomplete: "
                    + ", ".join(incomplete_upstream)
                )
            self.conformance_downstream_resume = True
            for name in (
                "conform-accompaniment",
                "mix-final",
                "validate-final-mix",
            ):
                stages[name] = {"status": "pending"}
            artifacts = self.manifest.get("artifacts")
            if isinstance(artifacts, dict):
                artifacts.pop("accompaniment-conformed.wav", None)
                artifacts.pop("final.wav", None)
            self.manifest.pop("final_mix_analysis", None)
            self.manifest["accompaniment_conformance"] = {
                "enabled": True,
                "succeeded": False,
                "source": None,
                "conformed": None,
                "anchors": [],
                "segments": [],
                "conformed_duration_seconds": None,
                "nominal_score_duration_seconds": self.manifest.get(
                    "nominal_score_duration_seconds"
                ),
            }

        configuration["accompaniment_conformance_enabled"] = requested_enabled
        configuration["accompaniment_conformance_anchors"] = requested_path
        configuration["accompaniment_conformance_anchors_sha256"] = requested_sha256
        inputs["conformance_anchors_sha256"] = requested_sha256

    def write_manifest(self) -> None:
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.manifest_path)

    def execute(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self.args.verbose:
            print("+", shlex.join(command), flush=True)
        result = subprocess.run(
            list(command),
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if self.args.verbose:
            if result.stdout:
                print(result.stdout, end="", file=sys.stdout, flush=True)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr, flush=True)
        if result.returncode != 0:
            raise PipelineError(
                f"command exited with status {result.returncode}: {shlex.join(command)}",
                exit_status=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return result

    def failure_details(self, error: BaseException) -> dict[str, Any]:
        details: dict[str, Any] = {
            "status": "failed",
            "error": str(error),
        }
        exit_status = getattr(error, "exit_status", None)
        if exit_status is not None:
            details["exit_status"] = exit_status
        stdout = getattr(error, "stdout", "")
        stderr = getattr(error, "stderr", "")
        if stdout:
            details["stdout"] = stdout
        if stderr:
            details["stderr"] = stderr
        return details

    def reset_stages_after(self, name: str) -> None:
        start = STAGE_NAMES.index(name) + 1
        for later_name in STAGE_NAMES[start:]:
            self.manifest["stages"][later_name] = {"status": "pending"}

    def stage(
        self,
        name: str,
        action: Callable[[], Any],
        validator: Callable[[], None] | None = None,
        *,
        always_run: bool = False,
    ) -> Any:
        self.current_stage = name
        previous_status = self.manifest["stages"][name].get("status")
        if self.args.resume and not always_run and validator is not None:
            try:
                validator()
            except Exception as error:
                if (
                    self.conformance_downstream_resume
                    and previous_status == "success"
                    and STAGE_NAMES.index(name)
                    < STAGE_NAMES.index("conform-accompaniment")
                ):
                    self.current_stage = "conform-accompaniment"
                    raise PipelineError(
                        "cannot enable accompaniment conformance because "
                        f"upstream stage verification failed: {name}"
                    ) from error
                if previous_status == "success":
                    self.manifest["stages"][name] = {"status": "pending"}
                    self.reset_stages_after(name)
            else:
                if previous_status == "success":
                    print(f"==> {name} (resume: verified, skipped)", flush=True)
                    self.manifest["stages"][name] = {"status": "success"}
                    self.write_manifest()
                    return None
        if (
            self.args.resume
            and not always_run
            and self.manifest["stages"][name].get("status") == "success"
        ):
            print(f"==> {name} (resume: skipped)", flush=True)
            return None
        print(f"==> {name}", flush=True)
        self.manifest["stages"][name] = {"status": "running"}
        self.write_manifest()
        try:
            result = action()
        except Exception as error:
            self.manifest["stages"][name] = self.failure_details(error)
            self.write_manifest()
            raise
        self.manifest["stages"][name] = {"status": "success"}
        self.write_manifest()
        return result

    def record_artifact(self, name: str, path: Path) -> None:
        require_nonempty_file(path, name)
        self.manifest["artifacts"][name] = {
            "path": str(path),
            "sha256": sha256_file(path),
        }

    def verify_artifact(self, name: str, path: Path, *, audio: bool = False) -> None:
        require_nonempty_file(path, name)
        record = self.manifest.get("artifacts", {}).get(name)
        if not isinstance(record, dict) or record.get("sha256") != sha256_file(path):
            raise PipelineError(f"recorded artifact does not match manifest: {name}")
        if audio:
            self.verify_audio(path)

    def analyze_final_mix(self, path: Path) -> None:
        result = self.execute(volumedetect_command(path))
        analysis = parse_volumedetect(result.stderr)
        self.manifest["final_mix_analysis"] = analysis
        validate_final_mix_analysis(analysis)

    def verify_final_mix_validation(self, path: Path) -> None:
        self.verify_artifact("final.wav", path, audio=True)
        analysis = self.manifest.get("final_mix_analysis")
        if not isinstance(analysis, dict):
            raise PipelineError("manifest has no final mix analysis")
        validate_final_mix_analysis(analysis)

    def verify_audio(self, path: Path) -> None:
        require_nonempty_file(path, "audio output")
        result = self.execute(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ]
        )
        try:
            probe = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise PipelineError(f"ffprobe returned invalid JSON for {path}: {error}") from error
        if not any(stream.get("codec_type") == "audio" for stream in probe.get("streams", [])):
            raise PipelineError(f"ffprobe found no audio stream in {path}")

    def audio_duration(self, path: Path) -> float:
        result = self.execute(duration_probe_command(path))
        return parse_audio_duration(result.stdout)

    def conform_accompaniment(
        self,
        source: Path,
        conformed: Path,
        nominal_duration: float,
    ) -> None:
        source_record = {
            "path": str(source),
            "sha256": sha256_file(source),
        }
        state = self.manifest.setdefault(
            "accompaniment_conformance",
            {
                "enabled": False,
                "succeeded": False,
                "source": None,
                "conformed": None,
                "anchors": [],
                "segments": [],
                "conformed_duration_seconds": None,
                "nominal_score_duration_seconds": None,
            },
        )
        state.update(
            {
                "enabled": self.args.accompaniment_conformance_anchors is not None,
                "succeeded": False,
                "source": source_record,
                "conformed": None,
                "anchors": [],
                "segments": [],
                "conformed_duration_seconds": None,
                "nominal_score_duration_seconds": nominal_duration,
            }
        )
        anchors_path = self.args.accompaniment_conformance_anchors
        if anchors_path is None:
            return

        anchors = manual_conformance_anchors(anchors_path)
        segments = conformance_segments(anchors, nominal_duration)
        state["anchors"] = [
            {
                "source_seconds": source_seconds,
                "destination_seconds": destination_seconds,
            }
            for source_seconds, destination_seconds in anchors
        ]
        state["segments"] = segments
        self.execute(
            conformance_command(source, conformed, segments, nominal_duration)
        )
        self.verify_audio(conformed)
        conformed_duration = self.audio_duration(conformed)
        state["conformed_duration_seconds"] = conformed_duration
        if not math.isclose(
            conformed_duration,
            nominal_duration,
            abs_tol=CONFORMED_DURATION_TOLERANCE_SECONDS,
        ):
            raise PipelineError(
                "conformed accompaniment duration "
                f"{conformed_duration:.9g} does not match nominal score duration "
                f"{nominal_duration:.9g}"
            )
        self.record_artifact("accompaniment-conformed.wav", conformed)
        state["conformed"] = dict(
            self.manifest["artifacts"]["accompaniment-conformed.wav"]
        )
        state["succeeded"] = True

    def verify_accompaniment_conformance(
        self,
        source: Path,
        conformed: Path,
        nominal_duration: float,
    ) -> None:
        state = self.manifest.get("accompaniment_conformance")
        if not isinstance(state, dict):
            raise PipelineError("manifest has no accompaniment conformance state")
        self.verify_artifact("accompaniment.wav", source, audio=True)
        if self.args.accompaniment_conformance_anchors is None:
            if state.get("enabled") or state.get("succeeded"):
                raise PipelineError("disabled accompaniment conformance state is invalid")
            return
        if not state.get("enabled") or not state.get("succeeded"):
            raise PipelineError("accompaniment conformance did not succeed")
        self.verify_artifact(
            "accompaniment-conformed.wav", conformed, audio=True
        )
        duration = state.get("conformed_duration_seconds")
        if not isinstance(duration, (int, float)) or not math.isclose(
            duration,
            nominal_duration,
            abs_tol=CONFORMED_DURATION_TOLERANCE_SECONDS,
        ):
            raise PipelineError(
                "manifest conformed duration does not match nominal score duration"
            )

    def ssh(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = remote_command(self.args.yue2_repository, arguments)
        return self.execute([*ssh_base_args(self.args), command])

    def preflight(self) -> None:
        file_requirements = (
            (self.args.melisma, "Melisma executable", True),
            (self.args.openutau_project, "OpenUtau CLI project", False),
            (self.args.midi, "MIDI input", False),
            (self.args.lyrics, "lyric input", False),
            (self.args.ssh_key, "SSH identity", False),
        )
        for path, description, executable in file_requirements:
            require_nonempty_file(path, description)
            if executable and not os.access(path, os.X_OK):
                raise PipelineError(f"{description} is not executable: {path}")
        if self.args.accompaniment_conformance_anchors is not None:
            require_nonempty_file(
                self.args.accompaniment_conformance_anchors,
                "accompaniment conformance anchors",
            )
        for program in ("dotnet", "ssh", "scp", "ffmpeg", "ffprobe"):
            if shutil.which(program) is None:
                raise PipelineError(f"required program is unavailable: {program}")

        midi_sha256 = sha256_file(self.args.midi)
        lyrics_sha256 = sha256_file(self.args.lyrics)
        inputs = self.manifest["inputs"]
        if self.args.resume:
            if inputs.get("midi_sha256") not in (None, midi_sha256):
                raise PipelineError("--resume MIDI content does not match the manifest")
            if inputs.get("lyrics_sha256") not in (None, lyrics_sha256):
                raise PipelineError("--resume lyric content does not match the manifest")
        inputs["midi_sha256"] = midi_sha256
        inputs["lyrics_sha256"] = lyrics_sha256
        anchors_path = self.args.accompaniment_conformance_anchors
        anchors_sha256 = sha256_file(anchors_path) if anchors_path is not None else None
        if self.args.resume and inputs.get("conformance_anchors_sha256") not in (
            None,
            anchors_sha256,
        ):
            raise PipelineError(
                "--resume accompaniment conformance anchors do not match the manifest"
            )
        inputs["conformance_anchors_sha256"] = anchors_sha256
        configuration = self.manifest["configuration"]
        configuration["accompaniment_conformance_enabled"] = (
            anchors_path is not None
        )
        configuration["accompaniment_conformance_anchors"] = (
            str(anchors_path) if anchors_path is not None else None
        )
        configuration["accompaniment_conformance_anchors_sha256"] = anchors_sha256

        try:
            self.ssh(
                [
                    "test",
                    "-f",
                    "skills/yue2-music/instrumental/scripts/instrumental.py",
                    "-a",
                    "-f",
                    "skills/yue2-music/instrumental/scripts/abc_tools.py",
                ]
            )
        except PipelineError as error:
            raise PipelineError(
                "RunPod SSH preflight failed; no pipeline stages were started.",
                exit_status=error.exit_status,
                stdout=error.stdout,
                stderr=error.stderr,
            ) from error

    def run(self) -> None:
        melisma_dir = self.output / "melisma"
        yue2_dir = self.output / "yue2"
        vocal = self.output / "vocal.wav"
        final = self.output / "final.wav"
        bound = melisma_dir / "bound.jsonl"
        ustx = melisma_dir / "vocal.ustx"
        abc = melisma_dir / "accompaniment.abc"
        abc_inspect = yue2_dir / "abc-inspect.json"
        prepared_inspect = yue2_dir / "prepared-inspect.json"
        verification = yue2_dir / "verification.txt"
        full_accompaniment = yue2_dir / "accompaniment-full.flac"
        accompaniment = yue2_dir / "accompaniment.wav"
        conformed_accompaniment = yue2_dir / "accompaniment-conformed.wav"

        self.write_manifest()
        self.stage("preflight", self.preflight, always_run=True)
        melisma_dir.parent.mkdir(parents=True, exist_ok=True)
        yue2_dir.mkdir(parents=True, exist_ok=True)

        def render_inputs() -> None:
            self.execute(melisma_command(self.args, melisma_dir))
            for name, path in (
                ("bound.jsonl", bound),
                ("vocal.ustx", ustx),
                ("accompaniment.abc", abc),
            ):
                self.record_artifact(name, path)

        self.stage(
            "melisma-render-inputs",
            render_inputs,
            lambda: [
                self.verify_artifact(name, path)
                for name, path in (
                    ("bound.jsonl", bound),
                    ("vocal.ustx", ustx),
                    ("accompaniment.abc", abc),
                )
            ],
        )

        def render_vocal() -> None:
            self.execute(openutau_command(self.args, ustx, vocal))
            self.verify_audio(vocal)
            self.record_artifact("vocal.wav", vocal)

        self.stage(
            "openutau-vocal-render",
            render_vocal,
            lambda: self.verify_artifact("vocal.wav", vocal, audio=True),
        )

        run_id = self.manifest.get("remote_run_id")
        if not isinstance(run_id, str) or not run_id:
            run_id = sanitize_run_id(self.output.name, self.args.seed)
        remote_run = str(PurePosixPath(self.args.yue2_repository) / "runs" / run_id)
        remote_abc = str(PurePosixPath(remote_run) / "accompaniment.abc")
        prepared_root = str(PurePosixPath(remote_run) / "prepared-run")
        prepared_dir = str(PurePosixPath(prepared_root) / "prepared")
        generation_dir = str(PurePosixPath(remote_run) / "generation")
        self.manifest["remote_run_id"] = run_id
        self.manifest["remote_run_directory"] = remote_run
        self.write_manifest()

        self.stage(
            "create-remote-run",
            lambda: self.ssh(create_remote_run_command(remote_run, self.args.resume)),
            lambda: self.ssh(["test", "-d", remote_run]),
        )

        def upload_abc() -> None:
            destination = (
                f"{self.args.runpod_user}@{self.args.runpod_host}:"
                f"{shlex.quote(remote_abc)}"
            )
            self.execute([*scp_base_args(self.args), str(abc), destination])

        self.stage(
            "upload-accompaniment-abc",
            upload_abc,
            lambda: self.ssh(["test", "-s", remote_abc]),
        )

        def inspect_uploaded() -> None:
            result = self.ssh(
                [
                    ".venv-instrumental/bin/python",
                    "skills/yue2-music/instrumental/scripts/abc_tools.py",
                    "inspect",
                    remote_abc,
                ]
            )
            abc_inspect.write_text(result.stdout, encoding="utf-8")
            require_nonempty_file(abc_inspect, "uploaded ABC inspection")
            duration = nominal_duration(result.stdout)
            self.manifest["nominal_score_duration_seconds"] = duration
            self.manifest["artifacts"]["abc-inspect.json"] = {
                "path": str(abc_inspect),
                "sha256": sha256_file(abc_inspect),
            }

        self.stage(
            "inspect-uploaded-abc",
            inspect_uploaded,
            lambda: (
                self.verify_artifact("abc-inspect.json", abc_inspect),
                nominal_duration(abc_inspect.read_text(encoding="utf-8")),
            ),
        )

        def prepare_yue2() -> None:
            result = self.ssh(
                [
                    ".venv-instrumental/bin/python",
                    "skills/yue2-music/instrumental/scripts/instrumental.py",
                    "cover",
                    "--abc",
                    remote_abc,
                    "--composer",
                    "agent",
                    "--style",
                    self.args.style,
                    "--empty-lyrics",
                    "--id",
                    run_id,
                    "--seed",
                    str(self.args.seed),
                    "--output",
                    prepared_root,
                    "--prepare-only",
                    "--offline",
                ]
            )
            score_path = prepared_score_path(prepared_dir, result.stdout)
            self.manifest["prepared_score_path"] = score_path

        self.stage(
            "prepare-yue2",
            prepare_yue2,
            lambda: self.ssh(
                [
                    "test",
                    "-f",
                    str(PurePosixPath(prepared_dir) / "prepared-manifest.json"),
                    "-a",
                    "-f",
                    prepared_score_path(prepared_dir),
                ]
            ),
        )

        def inspect_prepared() -> None:
            score_path = prepared_score_path(prepared_dir)
            result = self.ssh(
                [
                    ".venv-instrumental/bin/python",
                    "skills/yue2-music/instrumental/scripts/abc_tools.py",
                    "inspect",
                    score_path,
                ]
            )
            prepared_inspect.write_text(result.stdout, encoding="utf-8")
            require_nonempty_file(prepared_inspect, "prepared ABC inspection")
            json.loads(result.stdout)
            self.manifest["artifacts"]["prepared-inspect.json"] = {
                "path": str(prepared_inspect),
                "sha256": sha256_file(prepared_inspect),
            }

        self.stage(
            "inspect-prepared-score",
            inspect_prepared,
            lambda: self.verify_artifact("prepared-inspect.json", prepared_inspect),
        )

        self.stage(
            "generate-yue2",
            lambda: self.ssh(
                [
                    ".venv-instrumental/bin/python",
                    "skills/yue2-music/instrumental/scripts/instrumental.py",
                    "generate",
                    "--prepared",
                    prepared_dir,
                    "--output",
                    generation_dir,
                    "--models-root",
                    "models-instrumental",
                    "--device",
                    "cuda",
                    "--offline",
                ]
            ),
            lambda: self.ssh(["test", "-d", generation_dir]),
        )

        def verify_generation() -> None:
            result = self.ssh(
                [
                    ".venv-instrumental/bin/python",
                    "skills/yue2-music/instrumental/scripts/instrumental.py",
                    "verify",
                    generation_dir,
                ]
            )
            verification.write_text(
                result.stdout + (("\n" + result.stderr) if result.stderr else ""),
                encoding="utf-8",
            )
            require_nonempty_file(verification, "YuE2 verification output")
            self.manifest["artifacts"]["verification"] = {
                "path": str(verification),
                "sha256": sha256_file(verification),
            }

        self.stage(
            "verify-yue2-generation",
            verify_generation,
            lambda: self.verify_artifact("verification", verification),
        )

        expected_remote_audio = str(
            PurePosixPath(generation_dir) / "native" / "audio.flac"
        )

        def locate_generated_audio() -> None:
            listing = self.ssh(["find", generation_dir, "-type", "f", "-print"])
            generated_files = set(listing.stdout.splitlines())
            if expected_remote_audio not in generated_files:
                details = "\n".join(sorted(generated_files)) or "(no files found)"
                raise PipelineError(
                    f"expected generated audio is absent: {expected_remote_audio}\n"
                    f"generation files:\n{details}"
                )

        self.stage(
            "locate-generated-audio",
            locate_generated_audio,
            lambda: self.ssh(["test", "-s", expected_remote_audio]),
        )

        def download_audio() -> None:
            source = (
                f"{self.args.runpod_user}@{self.args.runpod_host}:"
                f"{shlex.quote(expected_remote_audio)}"
            )
            self.execute([*scp_base_args(self.args), source, str(full_accompaniment)])
            self.record_artifact("accompaniment-full.flac", full_accompaniment)

        self.stage(
            "download-full-accompaniment",
            download_audio,
            lambda: self.verify_artifact(
                "accompaniment-full.flac", full_accompaniment, audio=True
            ),
        )

        def extract_accompaniment() -> None:
            duration = self.manifest["nominal_score_duration_seconds"]
            self.execute(extraction_command(full_accompaniment, accompaniment, duration))
            self.verify_audio(accompaniment)
            self.record_artifact("accompaniment.wav", accompaniment)

        self.stage(
            "extract-accompaniment",
            extract_accompaniment,
            lambda: self.verify_artifact(
                "accompaniment.wav", accompaniment, audio=True
            ),
        )

        duration = self.manifest["nominal_score_duration_seconds"]
        self.stage(
            "conform-accompaniment",
            lambda: self.conform_accompaniment(
                accompaniment,
                conformed_accompaniment,
                duration,
            ),
            lambda: self.verify_accompaniment_conformance(
                accompaniment,
                conformed_accompaniment,
                duration,
            ),
        )

        def mix() -> None:
            conformance_state = self.manifest["accompaniment_conformance"]
            mix_accompaniment = accompaniment_for_mix(
                accompaniment,
                conformed_accompaniment,
                enabled=bool(conformance_state["enabled"]),
                succeeded=bool(conformance_state["succeeded"]),
            )
            self.execute(
                mix_command(
                    vocal,
                    mix_accompaniment,
                    final,
                    self.args.vocal_gain,
                    self.args.accompaniment_gain,
                    self.args.master_gain,
                )
            )
            self.verify_audio(final)
            self.record_artifact("final.wav", final)

        self.stage(
            "mix-final",
            mix,
            lambda: self.verify_artifact("final.wav", final, audio=True),
        )

        self.stage(
            "validate-final-mix",
            lambda: self.analyze_final_mix(final),
            lambda: self.verify_final_mix_validation(final),
        )
        self.manifest["status"] = "success"
        self.write_manifest()

    def record_failure(self, error: Exception) -> None:
        self.manifest["status"] = "failed"
        self.manifest["failed_stage"] = self.current_stage
        self.manifest["error"] = str(error)
        details = self.failure_details(error)
        for name in ("exit_status", "stdout", "stderr"):
            if name in details:
                self.manifest[name] = details[name]
        if self.current_stage in self.manifest["stages"]:
            self.manifest["stages"][self.current_stage] = details
        self.write_manifest()

    def record_interrupted(self) -> None:
        self.manifest["status"] = "interrupted"
        self.manifest["failed_stage"] = self.current_stage
        self.manifest["error"] = "pipeline interrupted by user"
        if self.current_stage in self.manifest["stages"]:
            self.manifest["stages"][self.current_stage] = {
                "status": "interrupted",
                "error": "pipeline interrupted by user",
            }
        self.write_manifest()


def build_parser() -> argparse.ArgumentParser:
    def connect_timeout(value: str) -> int:
        timeout = int(value)
        if not 1 <= timeout <= 120:
            raise argparse.ArgumentTypeError("must be between 1 and 120 seconds")
        return timeout

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--midi", required=True, type=Path)
    parser.add_argument("--lyrics", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--singer", required=True)
    parser.add_argument("--phonemizer", required=True)
    parser.add_argument("--renderer", required=True)
    parser.add_argument("--style", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--runpod-host", required=True)
    parser.add_argument("--runpod-port", required=True, type=int)
    parser.add_argument("--runpod-user", required=True)
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument(
        "--ssh-connect-timeout",
        type=connect_timeout,
        default=10,
        help="bounded RunPod SSH connection timeout in seconds",
    )
    parser.add_argument("--melisma", type=Path, default=Path(DEFAULT_MELISMA))
    parser.add_argument(
        "--openutau-project",
        type=Path,
        default=Path(DEFAULT_OPENUTAU_PROJECT),
    )
    parser.add_argument("--yue2-repository", default=DEFAULT_YUE2_REPOSITORY)
    parser.add_argument(
        "--vocal-gain",
        type=finite_positive_float,
        default=DEFAULT_VOCAL_GAIN,
        help="prototype mix gain (not normalization)",
    )
    parser.add_argument(
        "--accompaniment-gain",
        type=finite_positive_float,
        default=DEFAULT_ACCOMPANIMENT_GAIN,
        help="prototype mix gain (not normalization)",
    )
    parser.add_argument(
        "--master-gain",
        type=finite_positive_float,
        default=DEFAULT_MASTER_GAIN,
        help="prototype post-mix master gain (not normalization)",
    )
    parser.add_argument(
        "--accompaniment-conformance-anchors",
        type=Path,
        help=(
            "enable accompaniment timing conformance using a JSON array of "
            "manual source_seconds/destination_seconds anchor objects"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


def resolve_arguments(args: argparse.Namespace) -> argparse.Namespace:
    for name in (
        "midi",
        "lyrics",
        "output",
        "ssh_key",
        "melisma",
        "openutau_project",
    ):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    if args.accompaniment_conformance_anchors is not None:
        args.accompaniment_conformance_anchors = (
            args.accompaniment_conformance_anchors.expanduser().resolve()
        )
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = resolve_arguments(build_parser().parse_args(argv))
    try:
        ensure_output_directory(args.output, args.resume)
        pipeline = Pipeline(args)
        pipeline.run()
    except KeyboardInterrupt:
        pipeline_instance = locals().get("pipeline")
        if pipeline_instance is not None and args.output.is_dir():
            try:
                pipeline_instance.record_interrupted()
            except OSError as manifest_error:
                print(
                    f"could not write interrupted manifest: {manifest_error}",
                    file=sys.stderr,
                )
        print("pipeline interrupted by user", file=sys.stderr)
        return 130
    except Exception as error:
        pipeline_instance = locals().get("pipeline")
        if pipeline_instance is not None and args.output.is_dir():
            try:
                pipeline_instance.record_failure(error)
            except OSError as manifest_error:
                print(f"could not write failure manifest: {manifest_error}", file=sys.stderr)
        stage = (
            pipeline_instance.current_stage
            if pipeline_instance is not None
            else "initialization"
        )
        print(f"pipeline failed during {stage}: {error}", file=sys.stderr)
        stdout = getattr(error, "stdout", "")
        stderr = getattr(error, "stderr", "")
        if stdout:
            print(stdout.rstrip(), file=sys.stderr)
        if stderr:
            print(stderr.rstrip(), file=sys.stderr)
        return 1
    print(f"pipeline completed: {args.output / 'final.wav'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
