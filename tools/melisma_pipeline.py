#!/usr/bin/env python3
"""Orchestrate the prototype Melisma vocal and YuE2 accompaniment pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
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
DEFAULT_VOCAL_GAIN = 0.25
DEFAULT_ACCOMPANIMENT_GAIN = 4.0

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
    "mix-final",
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
) -> list[str]:
    filter_graph = (
        f"[0:a]volume={format(vocal_gain, '.15g')}[vocal];"
        f"[1:a]volume={format(accompaniment_gain, '.15g')}[accompaniment];"
        "[vocal][accompaniment]amix=inputs=2:normalize=0:duration=longest[mix]"
    )
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(vocal),
        "-i",
        str(accompaniment),
        "-filter_complex",
        filter_graph,
        "-map",
        "[mix]",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]


class Pipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output = args.output
        self.manifest_path = self.output / "manifest.json"
        self.current_stage = "initialization"
        if args.resume:
            self.manifest = self.load_manifest()
            self.prepare_resumed_manifest()
        else:
            self.manifest = self.new_manifest()

    def new_manifest(self) -> dict[str, Any]:
        return {
            "status": "running",
            "inputs": {
                "midi": str(self.args.midi),
                "lyrics": str(self.args.lyrics),
                "midi_sha256": None,
                "lyrics_sha256": None,
            },
            "configuration": {
                "singer": self.args.singer,
                "phonemizer": self.args.phonemizer,
                "renderer": self.args.renderer,
                "style": self.args.style,
                "seed": self.args.seed,
                "vocal_gain": self.args.vocal_gain,
                "accompaniment_gain": self.args.accompaniment_gain,
                "melisma": str(self.args.melisma),
                "openutau_project": str(self.args.openutau_project),
                "remote_yue2_repository": self.args.yue2_repository,
                "remote_host": self.args.runpod_host,
                "remote_port": self.args.runpod_port,
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
            except Exception:
                if previous_status == "success":
                    self.manifest["stages"][name] = {"status": "pending"}
                    self.reset_stages_after(name)
            else:
                if previous_status != "pending":
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

        def mix() -> None:
            self.execute(
                mix_command(
                    vocal,
                    accompaniment,
                    final,
                    self.args.vocal_gain,
                    self.args.accompaniment_gain,
                )
            )
            self.verify_audio(final)
            self.record_artifact("final.wav", final)

        self.stage(
            "mix-final",
            mix,
            lambda: self.verify_artifact("final.wav", final, audio=True),
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
        type=float,
        default=DEFAULT_VOCAL_GAIN,
        help="prototype mix gain (not normalization)",
    )
    parser.add_argument(
        "--accompaniment-gain",
        type=float,
        default=DEFAULT_ACCOMPANIMENT_GAIN,
        help="prototype mix gain (not normalization)",
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
