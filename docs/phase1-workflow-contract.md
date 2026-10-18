# Phase 1 workflow and tool-interface contract

## Issue #7 implementation extension (October 18, 2026)

The local artifact catalog wraps the Issue #2 validation records and the Issue
#6 registry; it does not replace adapters or add a scheduler. `Operation.inputs`
records explicit input roles, paths, artifact IDs and consumed content revisions.
Preview is read-only; Start checks revisions and registers inputs. Finalized
results register producing operation/run/session, output validation and separate
production status. Rejected occupied destinations are not claimed as outputs.
Native YuE2 song audio remains semantically distinct from proven instrumental
helper output.

Versioned atomic local history and bounded diagnostic files survive console
eviction/restart. History corruption/version/write errors are explicit; duplicate
canonical path identities are rejected, not merged. Recent selection uses
server-defined input compatibility while preserving manual paths.
Text inspection never writes; bounded/truncated or binary/non-UTF-8 views cannot
be edited. Explicit edits default to no-clobber Save As, which also refuses
catalogued paths whose files are missing. Complete private temporary content is
fsynced before publication. In-place replacement requires the inspected revision,
preserves ordinary permissions and first durably publishes a private backup.
History and text publication fsync their containing directories; subsequent
failures retain and explicitly report the published filesystem outcome.
Known consumers of older revisions are flagged without changing historical
execution success.
Derived copies do not invalidate their unchanged source.

Configuration/receipt/diagnostic/IR text is read-only initially. Native desktop
launching, prototype-manifest migration, remote discovery and history pruning
are not implemented. Issue #8 retains transitive dependencies/settings
invalidation/rerun planning; #9 retains audio UI execution; #10 retains Full
Pipeline. See README's Issue #7 section for persistence and API contracts.

## Issue #6 implementation extension (October 16, 2026)

The original inspection below remains historical evidence, not a claim of
current runtime readiness. Issue #2 supplied the adapter/process framework;
Issue #5 supplied workflow-aware forms; Issue #6 adds the following bounded
local execution contract. See README's "Command preview, session records, and
Stop" for API payloads, configuration examples and test commands.

Only Generate ABC and Generate USTX are connected to web execution. Their
trusted construction path is `ExecutionAPI.construct -> MelismaAdapter.build
(publish_stdout=True) -> immutable Operation/Command`. Preview retains this
operation under an opaque ID; Start consumes it, rechecking the selected
nonempty/readable local input paths without changing the command. At most 32
pending previews survive, oldest first evicted. Neither form availability nor
configured source presence proves executable identity, input compatibility,
installed resources or audio readiness. Browser configuration drafts do not
change active executable/root authorization. Exactly one configured local
Melisma source with an explicit executable and supported contract is required.

The command preview includes executable, ordered arguments, cwd, display-only
shell quoting, expected artifact records and operation metadata. Execution
always uses structured argv and `shell=False`, never the display string.
Environment overrides and their values are omitted from preview, run and
validation-process command documents.
Browser submissions cannot select executable, argv, shell, cwd, environment
or arbitrary PID. Only explicitly entered absolute input/output paths and
existing ABC/USTX metadata fields are accepted. Browser file-picker names are
not converted to local host paths. These deliberate local file reads/writes
are not a sandbox against an untrusted operator.

For the web's single-file outputs, Melisma's existing stdout mode avoids the
legacy CLI's overwriting `fopen("w")` path. The adapter exclusively creates the
expected destination after process success and complete capture, refusing
occupied paths including symlinks/publication races. The display command prints the artifact;
the adapter publication step is separately documented in metadata. No C flags
or command templates are invented. Publication copies the complete raw binary
stdout spool, never the bounded or decoded console prefix. Console truncation
does not change the artifact's functional output-size limit.
Existing internal adapter file-output behavior remains unchanged.

`ProcessState` now includes created, starting, running, exited, signalled,
start-failed, rejected and cancelled-before-start. `ProcessResult` retains
separate stdout/stderr, PID, exit/signal, original start error, Stop intent,
escalation, UTC ISO 8601 submission/start/completion times, monotonic duration,
byte counts, stream truncation and EOF-completeness flags. Process exit is not
operation success: artifact inspection, validation errors, completion metadata and validation
subprocess records remain separate. Run `complete` is published after adapter
finalization; phase `validating` can follow primary process exit.

`OwnedChild` is the shared process primitive for synchronous calls, playback
and asynchronous session execution. It drains two independent pipes, retaining
the first 1 MiB of each as a bounded console prefix with explicit truncation.
ProcessRunner and session adapter execution separately spool complete raw
stdout/stderr into private temporary files, deleted at command finalization.
All machine parsers receive complete text; retained session/API primary and
validation records remain bounded. Spooling uses disk proportional to output;
machine text transiently uses memory. Resources/filesystem capacity, not
console retention, bounds functional output. Diagnostic-only playback capture
does not need complete spools. Captured console data survives completion.
The synchronized session registry admits at most four active runs and retains
at most 32 runs in submission order, evicting the oldest submitted completed
run first and never evicting a running child. Reads return snapshots.
`GET /api/runs` lists retained records; `GET /api/runs/ID` provides incremental
output. UI polling uses 500 ms and ends when complete or visibly failed.
No logs survive server restart.

Guarded JSON POST routes are `/api/operations/preview`, `/api/runs` and
`/api/runs/ID/stop`, in addition to existing configuration/refresh routes.
They retain exact loopback Host, same-origin/CSRF, bounded body and strict JSON
validation. Stop accepts only an empty object, targets the exact owned child,
waits up to one second for graceful termination, escalates only if necessary,
and reaps it. Internal OwnedChild retains its bounded `(0,5]` stop timeout.
Stop intent is not conflated with exit code (including a handled exit 0);
repeated Stop/completed-run Stop is safe. Finalization cannot be rolled back
by late Stop. Shutdown closes admission, stops children and joins workers.
Descendants are not killed. After observing child exit, readers drain and wait
for EOF for up to 0.5 seconds, including idle intervals. An inherited writer
preventing EOF produces an explicit incomplete-capture error, closes the reader,
retains byte counts/prefix/truncation state and prevents success/publication.
EOF completeness and console truncation are independent.
Output and diagnostic DOM rendering uses text, never HTML interpretation.

The Issue #7 extension above supplies cross-session artifact history, provenance
and direct consumed-revision warnings without a rerun model. Issue #9 retains all
tool-specific audio UI execution, including playback and native/helper YuE2;
those contracts remain distinct and unavailable here. Issue #10 retains
full-pipeline coordination. Future MIDI generation remains unresolved.

Discovery for GitHub Issue #1, inspected **October 13, 2026**. This is a
snapshot of the local interfaces, not a harness implementation or an
end-to-end rendering acceptance test. No adapters, UI, orchestration code,
dependency installation, model downloads, or commits were made.

## 1. Evidence, environment, and verification levels

Labels used throughout:

- **Verified current behavior (V):** observed through safe execution or
  read-only artifact inspection during this investigation.
- **Available but not runtime-verified behavior (S):** established by current
  source, argument parsers, or tests; the producing operation was not run.
- **Not currently supported / interface gap (G):** absent from the inspected
  interface, unavailable in the inspected environment, or explicitly deferred.

An accepted CLI argument is not evidence that its dependencies are installed.
Existing audio is not evidence that it can be regenerated with today's tools.
Command templates below are **not executed generation commands** unless
explicitly marked V. Uppercase shell variables denote operator-supplied,
validated paths/values, not new tool flags. Future execution should use
structured argument arrays rather than shell interpolation.

| Inspected location/tool | Revision/version and state |
| --- | --- |
| `/home/jeffbarnes/melisma` | Branch `main`, HEAD `940087fb35014e35f51a3c433f7eae0da81a88f6` (`docs: add Melisma README`); existing `build/melisma` executable ran successfully. No CLI version flag. |
| `/home/jeffbarnes/OpenUtau-CLI` | HEAD `efcdabee06b3b7e54d297832c82d8a498de7041e` (`docs: document headless OpenUtau CLI`); existing Release executable ran its help. Pre-existing untracked `console_out.txt` untouched. |
| `/home/jeffbarnes/YuE` | HEAD `700f22cbed676e69c05d853b964a576ceed937d2` (`tools: test native ABC slur correspondence`); clean Git status at inspection. This checkout is YuE2, not the older YuE-v1 interface. |
| `/usr/bin/ffmpeg`, `/usr/bin/ffplay` | Both `6.1.1-3ubuntu5`, built with GCC 13; version output inspected. |
| `/usr/bin/ffprobe` | Present and successfully read the reference WAVs. Used for artifact validation, not a separate generation stage. |
| .NET | Installed `Microsoft.NETCore.App` and `Microsoft.AspNetCore.App` `8.0.31`; CLI target is `net8.0`. No build/restore performed. |
| Python | Active `python3` is `3.12.3`; `/usr/bin/python3.12` exists. YuE2 source/package metadata declares `0.1.6`, Python `>=3.10`. |
| Active Python inference environment | No `yue2` executable on PATH; no installed `yue2-infer`, Torch, Transformers, Hugging Face Hub, SoundFile, Tiktoken, Safetensors, or Accelerate distributions. NumPy is `1.26.4`, not the checkout's pinned `2.2.6`. |
| Local YuE2 environment/model candidates | No matches for `YuE/.venv*/bin/python*` or `YuE/models*/**/config.json`. `YUE2_KIT` is unset. This is not a search of all environments or Hugging Face caches on the machine. |
| Linux OpenUtau data configuration | `XDG_DATA_HOME` and `XDG_CACHE_HOME` are unset in the command environment. The source-defined external paths are described in section 3; their contents were **not inspected**. |

Repository revisions identify source snapshots, not provenance of ignored
build products. Neither existing binary was rebuilt or proven byte-for-byte
to correspond to HEAD.

The initial untracked Melisma files were preserved: `binder.test`,
`create-phase1-issues.sh`, `implementation_checklist.md`, `lyric.test`,
`note.txt`, `reference/`, `requirements.md`, and `tiger-a-c4.wav`. Requirements
and the checklist describe intent; executable interfaces and source take
precedence. The only new repository file is this document.

## 2. Melisma command contracts

### Shared input, parsing, and failure contract

**Tool/entry point:** `/home/jeffbarnes/melisma/build/melisma`.
**Working directory:** any directory with absolute paths; templates using
`./build` and `examples/` require `/home/jeffbarnes/melisma`.
**Required inputs:** a readable MIDI file and a readable Melisma lyric file,
even when selecting ABC, whose serializer does not consume lyric bindings.
No stdin input mode or direct textual note-input CLI exists.

The importer accepts Standard MIDI **format 0, one track, positive PPQN
division**, monophonic non-overlapping notes with matching channel/pitch
note-offs and positive durations. SMPTE division, format 1/2, concurrent
notes, and malformed containers/events are rejected. Note-on with velocity
zero is a note-off. Running status is handled. Other supported channel
messages are skipped; metadata, including tempo/meter, is skipped rather
than preserved. The represented endpoint is the last note's end, not an
arbitrary trailing end-of-track delay.

Lyric grammar: `[A-Za-z]+` is an onset token, `--` consumes one continuation
note, `_` consumes one untexted note, and a single final `|` is optional.
Whitespace, single `-`, and `#` comments are ignored. Three or more adjacent
hyphens and unsupported characters are invalid. The grammar requires a
nonempty element sequence. The required note count is onsets plus
continuations plus untexted elements, and must equal the imported note count.
This is not a free-form Unicode song-lyrics interface.

Options must precede positional arguments. Duplicate switches, missing/empty
option values, unknown formats/switches, and leading-hyphen positional paths
are rejected; there is no `--` end-of-options handling.

| Exit | Meaning from `src/main.c` |
| ---: | --- |
| `0` | Selected serialization completed and stream flush/close succeeded. |
| `1` | MIDI import, lyric parse, binding, serialization, or bundle-publication failure. |
| `2` | Usage error or failure opening single-command MIDI, lyric, or output files. |

Errors go to stderr. Single-file output uses `fopen(..., "w")`, so it can
overwrite an existing file. A serialization failure removes that output;
never aim a template at a valuable existing artifact. Bundle behavior is
different, as documented below.

**Capability discovery:** `./build/melisma` with no arguments prints usage
and exits 2. `--help` and `--version` were tested: each prints usage and exits
2, rather than implementing a help/version command. There is no JSON schema,
format-list command, singer lookup, or automatic installed-default query.
Source/parser inspection is the fallback for defaults and format values.

Evidence: `src/main.c:1-26, 260-455`, `src/midi.c:180-380`,
`src/melisma_lexer.l`, `src/melisma_parser.y`, `src/binder.c`;
`tests/cli_test.c` contains exact expected Gloria IR/ABC/USTX.

### Operation: MIDI handling versus MIDI generation

| Contract field | Current interface |
| --- | --- |
| Tool / operation | Melisma: import MIDI and bind lyrics (**V**); generate/export MIDI (**G**). |
| Entry point / working directory | Shared executable and cwd above. |
| Inputs / options / defaults | Shared MIDI and lyric inputs; default format `ir`. No `--format midi`, compose, note-text, or MIDI-output switch. |
| Outputs | Bound JSON Lines on stdout, or the selected IR file; **no new MIDI**. |
| Capability discovery | Usage and `enum ml_output_format` in `src/main.c`; importer source. |
| Verified command | `./build/melisma examples/five-notes.mid examples/gloria.txt` |
| Success criteria | Exit 0, header with PPQN 480, five bound notes with pitches 60/62/64/65/67 and matching lyric assignments. Observed. |
| Known gaps | Phase 1's conceptual "Generate MIDI" workflow has no producer in this implementation. An existing MIDI can be selected/imported, not fabricated as a generated artifact. |

For the observed fixture, onset ticks are `0, 480, 960, 1440, 1920`, each
duration is 480, and the bindings are `Glo`, `ri`, `a`, continuation of `a`,
continuation of `a`. IR header version is 1. At the serialized fixed 120 BPM,
the last note ends at 2.5 seconds.

### Operation: generate ABC

| Contract field | Current interface |
| --- | --- |
| Tool / operation | Melisma: MIDI + lyric validation to ABC (**V** on stdout, **S** for file output). |
| Entry point / working directory | Shared executable/cwd. |
| Inputs | Shared MIDI/lyric files; optional third positional output path. |
| Options | `--format abc`; USTX metadata switches are not permitted in this mode. |
| Defaults | Fixed `M:4/4`, `L:1/32`, `Q:1/4=120`, `K:C`, blank title; voices `Vocal` and `Ins`. No tempo/key/meter overrides. |
| Outputs | ABC stdout or exactly the requested file. All MIDI notes appear in `Vocal`; `Ins` is rests. No `w:` lyrics, syllable/slur binding, or chord generation. |
| Capability discovery | Usage, `src/abc.c`, and `tests/abc_test.c` / `tests/cli_test.c`. |
| Verified command | `./build/melisma --format abc examples/five-notes.mid examples/gloria.txt` |
| File template (not run) | `./build/melisma --format abc "$MIDI" "$MELISMA_LYRICS" "$ABC_OUTPUT"` |
| Success criteria | Exit 0; complete ABC with both voices, expected pitches/timing and padding. For file mode also require a new nonempty parseable file. |
| Known gaps | Despite the bundle name `accompaniment.abc`, this is a melody projection, not composed accompaniment or an instrumental-only ready score. Lyrics still must bind successfully. |

Onsets and durations must be exactly representable in eight units per quarter
note (a 1/32-note grid). The serializer rejects nonrepresentable timing,
preserves gaps with rests, ties notes split across bar/group boundaries,
groups at most four measures, and pads the final measure. The verified
five-note body is:

```abc
V: Vocal
=C8 =D8 =E8 =F8 | =G8 z24 |
V: Ins
Z2 |
```

Thus the complete ABC spans two 4/4 bars: **4.0 seconds**, although its last
sounding note ends at 2.5 seconds. Evidence: `src/abc.c:1-96, 309-406`.

### Operation: generate USTX

| Contract field | Current interface |
| --- | --- |
| Tool / operation | Melisma: bound MIDI/lyrics to USTX (**V** on stdout; file writing **S**). |
| Entry point / working directory | Shared executable/cwd. |
| Inputs | MIDI, Melisma lyrics; optional positional output file. |
| Options | `--format ustx`, optional `--singer NAME`, `--phonemizer CLASS`, `--renderer NAME`. Strings are serialized, not resolved against OpenUtau. |
| Defaults | All three metadata strings empty; USTX `0.7`, BPM 120, 4/4; resolution from MIDI PPQN; track `Track1`, part at tick 0. |
| Outputs | USTX stdout or exact requested filename. Onsets retain lyric text, continuations become `"+"`, `_` notes are omitted from the vocal note list while absolute timing/part endpoint remain. |
| Capability discovery | Usage and `src/ustx.c`; external resources must be validated separately. |
| Success criteria | Exit 0 plus parseable USTX; expected resolution, singer/phonemizer/renderer metadata and note positions. This alone does **not** establish renderability. |
| Known gaps | Empty singer is not an installed-singer default. No USTX-input editing, singer discovery, render execution, or tempo override. |

Exact command executed successfully, producing stdout only:

```sh
./build/melisma --format ustx \
  --singer TIGER_DS_v102 \
  --phonemizer OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer \
  --renderer DIFFSINGER \
  examples/five-notes.mid examples/gloria.txt
```

File-output template: append `"$USTX_OUTPUT"` to the corresponding arguments.
The values above are a **reference preset**, not discovered installed
capabilities. Evidence: `src/ustx.c:60-239`; reference `first.ustx:235-238`.

### Operation: multi-output renderer-input bundle

| Contract field | Current interface |
| --- | --- |
| Tool / operation | Melisma `render-inputs` (**S**; command/parser/tests inspected, directory-producing operation deferred). |
| Entry point / working directory | Shared executable/cwd. |
| Inputs | MIDI, Melisma lyrics, mandatory output directory. Its parent must exist. |
| Options | Optional `--singer`, `--phonemizer`, `--renderer`; **no `--format`** in this subcommand. |
| Defaults | Same serializer metadata and musical defaults above. |
| Outputs | Exactly `bound.jsonl`, `vocal.ustx`, `accompaniment.abc` in the requested directory. One import/parse/bind feeds all outputs. |
| Capability discovery | Usage and `tests/render_inputs_cli_test.c`; no separate manifest/schema discovery. |
| Success criteria | Exit 0; all three nonempty, mutually consistent artifacts; no leftover temporary files. |
| Known gaps | No MIDI output, no audio, no structured capability response. C's directory creation is not recursive. |

Source-backed template, not executed:

```sh
./build/melisma render-inputs \
  --singer "$SINGER_ID" --phonemizer "$PHONEMIZER_CLASS" --renderer "$RENDERER_ID" \
  "$MIDI" "$MELISMA_LYRICS" "$NEW_BUNDLE_DIR"
```

The directory must be absent or empty. Nonempty directories are refused.
Each artifact is first written/flushed/closed under a hidden PID-suffixed
temporary name, then published with rename. Handled failures remove temporary
and already-published files; the directory can remain. Publication is not a
single transactional rename of the complete bundle, and abrupt process death
is not covered by that cleanup. Evidence: `src/main.c:68-253`;
`tests/render_inputs_cli_test.c:225-380`.

### Operation: existing experimental end-to-end pipeline

There is **already orchestration code**, independently of the future harness.
The C executable does not render audio, but `tools/melisma_pipeline.py` does
coordinate external stages. Do not confuse it with a new local Phase 1 backend.

| Contract field | Current interface |
| --- | --- |
| Tool / operation | Melisma Python prototype: local Melisma/OpenUtau, remote YuE2 through SSH/SCP, audio extraction/conformance/mix (**V** help; execution **S**). |
| Entry point / working directory | `python3 -B tools/melisma_pipeline.py`; cwd Melisma root for relative defaults. Input/tool/key paths are expanded and resolved locally. Remote commands explicitly `cd` to the YuE2 repository. |
| Required inputs | `--midi`, `--lyrics`, `--output`, `--singer`, `--phonemizer`, `--renderer`, `--style`, integer `--seed`, `--runpod-host`, integer `--runpod-port`, `--runpod-user`, `--ssh-key`. No local-only mode. |
| Optional options | `--melisma`, `--openutau-project`, `--yue2-repository`, `--ssh-connect-timeout`, `--vocal-gain`, `--accompaniment-gain`, `--master-gain`, `--accompaniment-conformance-anchors`, `--verbose`, `--resume`. |
| Defaults | Melisma `./build/melisma`; OpenUtau project `~/OpenUtau-CLI/OpenUtau.Cli/OpenUtau.Cli.csproj`; **remote** YuE2 root `/workspace/YuE`; SSH timeout 10 seconds (valid 1..120); gains vocal 0.5, accompaniment 1.0, master 0.707945784; conformance/resume/verbose disabled. |
| Outputs | Local `manifest.json`, bundle `melisma/`, `vocal.wav`, `yue2/abc-inspect.json`, `prepared-inspect.json`, `verification.txt`, `accompaniment-full.flac`, `accompaniment.wav`, optional `accompaniment-conformed.wav`, and `final.wav`; remote prepared/generation artifacts retained separately. |
| Discovery | `python3 -B tools/melisma_pipeline.py --help`; parser/source for default values. Preflight is **not** a read-only local capability query: execution creates output/manifest and attempts SSH. |
| Success / exit | Main returns 0 after all stages, 1 on failure, 130 on keyboard interruption; failures and manifest preserved. Audio must be nonempty with an audio stream; final peak reaching 0.0 dBFS fails validation. |
| Gaps | Remote endpoint/key/environment required; no ffplay stage, MIDI generation, general stage-selector UI, or local YuE2 switch. It does not discover singers/models automatically. |

Source-backed complete required-argument template, not executed:

```sh
python3 -B tools/melisma_pipeline.py \
  --midi "$MIDI" --lyrics "$MELISMA_LYRICS" --output "$NEW_RUN_DIR" \
  --singer "$SINGER_ID" --phonemizer "$PHONEMIZER_CLASS" --renderer "$RENDERER_ID" \
  --style "$INSTRUMENTAL_STYLE" --seed "$SEED" \
  --runpod-host "$REMOTE_HOST" --runpod-port "$REMOTE_PORT" \
  --runpod-user "$REMOTE_USER" --ssh-key "$SSH_KEY"
```

It uses `dotnet run --project ... --no-build -- render ... --out ...`;
without a configuration argument this expects the default Debug build,
whereas the directly inspected executable here was Release. Remote preparation
uses `.venv-instrumental/bin/python` and the instrumental helper described in
section 4; generation uses `models-instrumental` and CUDA with `--offline`.
Those are remote assumptions, not verified local installations.
The output directory must be empty/new unless `--resume` validates the
manifest and artifact hashes. Optional manual conformance uses JSON
`source_seconds`/`destination_seconds` anchors, not automatic note alignment.
Commands capture stdout/stderr in memory; `--verbose` prints them, and failed
commands' diagnostics are stored in the manifest. There is no `logs/`
directory or complete persistent log of every successful command. A future
harness must provide that separately rather than assume it already exists.

Evidence: `tools/melisma_pipeline.py:20-47, 139-180, 425-495, 510-561,
735-781, 840-891, 999-1385, 1414-1538`; `tests/test_melisma_pipeline.py` is principally
command/manifest behavior with mocked external execution, not GPU acceptance.

## 3. OpenUtau-CLI vocal rendering contract

| Contract field | Current interface |
| --- | --- |
| Tool / operation | OpenUtau-CLI: USTX project to complete WAV mixdown (**V** help; actual render **S**). |
| Executable / entry point | `/home/jeffbarnes/OpenUtau-CLI/OpenUtau.Cli/bin/Release/net8.0/openutau-cli`; equivalent managed DLL with `dotnet`; source entry `OpenUtau.Cli/Program.cs`. |
| Working directory | Any with absolute input/output paths; source `dotnet run --project ...` examples use the repository root. Wave-part dependencies resolve relative to the USTX project. |
| Inputs | Existing `.ustx`, its referenced voicebank/models/plugins/wave files, existing output parent directory. |
| Options | Exactly `render INPUT.ustx --out OUTPUT.wav`, in that order. Extension checks case-insensitive; extra arguments refused. Only top-level `--help` is a help operation. |
| Defaults | No default input/output path. Singer/phonemizer/renderer come from USTX/core preferences, not flags. Mixdown writes 44,100 Hz stereo 16-bit WAV. |
| Outputs | Exactly the `--out` WAV path; no per-track suffixing or automatic per-track export. Rendering can also use/write external runtime caches. Existing output may be overwritten; partial output is not explicitly removed on failure. |
| Capability discovery | `openutau-cli --help` is safe: it returns before headless initialization. No `list-singers`, `list-phonemizers`, `list-renderers`, JSON capabilities, or CLI `--version` branch exists. |
| Success criteria | Exit 0 **and** a new nonempty decodable WAV with audio and expected duration/content; help exit 0 alone is not render success. |
| Known gaps | No `--singer`, `--phonemizer`, `--renderer`, `--device`, data-root, track-selection, or output-format switches. External TIGER installation was not inspected or rendered. |

Verified command:

```sh
/home/jeffbarnes/OpenUtau-CLI/OpenUtau.Cli/bin/Release/net8.0/openutau-cli --help
```

Source-backed render template, **not run**:

```sh
/home/jeffbarnes/OpenUtau-CLI/OpenUtau.Cli/bin/Release/net8.0/openutau-cli \
  render "$USTX_INPUT" --out "$NEW_VOCAL_WAV"
```

Exit 1 means invalid command/arguments and writes usage/error to stderr.
Exit 2 means loading, validation, render, or export failure, with an error and
immediate inner-exception message. The service verifies singers for used voice
tracks, waits for phonemization, checks phonemizer errors and renderer
availability, then invokes the existing render engine. The executable does
not wire a cancellation token or dedicated graceful Ctrl+C handler even
though the service accepts `CancellationToken`.

### External runtime/configuration and discovery boundary

These paths are **source-defined, not inspected installations**:

| Resource | Exact source mechanism | Purpose and safety |
| --- | --- | --- |
| Linux data root | `OpenUtau.Core/Util/PathManager.cs:34-49`: `${XDG_DATA_HOME}/OpenUtau`, otherwise `~/.local/share/OpenUtau`. | Headless rendering and installed-resource lookup share these paths with desktop OpenUtau; this is not merely desktop compatibility. |
| Singer roots | `PathManager.cs:79-105`: `Singers`, existing legacy `Content/Singers`, and existing `Preferences.Default.AdditionalSingerPath`. | `Classic/ClassicSingerLoader.cs:21-28` and `SingerManager.cs:25-32` use these roots for headless discovery. |
| Singer identity/type | `Classic/VoicebankLoader.cs:44-120, 125-174`: enumerate `character.txt`; optional `character.yaml`; relative directory path is singer ID; config or `dsconfig.yaml` identifies DiffSinger. | A future explicitly authorized read-only filesystem scanner can discover candidate IDs without hard-coding TIGER. Mere directory presence is not rendering validation. Search depth is preference-dependent. |
| DiffSinger assets | `DiffSinger/DiffSingerSinger.cs:73-120, 165-170`: voicebank-local `dsconfig.yaml`, named phoneme/language/acoustic resources; additional pitch/variance/vocoder models as configured. | These are actual renderer dependencies. No installed TIGER paths/model integrity were checked. |
| Preferences | `Util/Preferences.cs:15-44, 100-126, 153-178`: data-root `prefs.json`, possibly shipped `prefs-default.json`. | Loading can reset/save preferences if absent/invalid. Do not invoke initialization as supposedly read-only discovery. |
| Plugins/phonemizers | `DocManager.cs:74-133`: built-in DLL beside executable, external `Plugins/**/*.dll`, and exported core phonemizer types. | `SearchAllPlugins()` creates directories and may **delete an old built-in DLL** in the plugin directory. It was not called. |
| Cache | `PathManager.cs:43-49`: `${XDG_CACHE_HOME}/OpenUtau`, otherwise `~/.cache/OpenUtau`. | Rendering cache, not a repository artifact directory. No cache inspection or cleanup performed. |

**The installed TIGER singer location/configuration was not runtime-inspected.**
Access to `/home/jeffbarnes/.local/share/OpenUtau` was declined and then
excluded by user direction. No contents were obtained, no alternative access
was attempted, and no discovery/initialization API with filesystem side
effects was invoked. `TIGER_DS_v102` in reference files proves only the saved
selection, not current installation.

The core exposes `SingerManager.Singers`, `PhonemizerFactory` registrations,
and `Renderers.GetSupportedRenderers(USingerType)`, but none is a safe public
CLI enumeration. Source lists Classic `[WORLDLINE-R, CLASSIC]`, Enunu
`[ENUNU]`, Vogen `[VOGEN]`, DiffSinger `[DIFFSINGER]`, Voicevox `[VOICEVOX]`.
`WORLDLINE-R2` is constructible but not in that supported-list array.
Distinguish source-defined renderer kinds from installed functional backends.

`Ustx/UTrack.cs:212-248` falls back to `DefaultPhonemizer` if the named
phonemizer cannot be resolved; empty renderer chooses a singer-type default.
No automatic CLI selection of a bank's `DefaultPhonemizer` is established by
this load path. In particular, unknown phonemizer text can lead to fallback,
not necessarily a hard failure. Validate the intended class before rendering.
The reference class exists at
`OpenUtau.Core/DiffSinger/Phonemizers/DiffSingerARPAPlusEnglishPhonemizer.cs`.

For DiffSinger the type-based renderer default is `DIFFSINGER`.
Linux `OpenUtau.Core/Util/Onnx.cs:35-55, 88-128` advertises CPU only; do not
invent a CUDA CLI option. Source preference defaults include depth 1.0,
20 acoustic steps, 20 variance steps, and 10 pitch steps. Actual external
preference values remain unknown.

Primary evidence: `OpenUtau.Cli/Program.cs:7-109`,
`OpenUtau.Cli/OpenUtau.Cli.csproj`,
`OpenUtau.Core/Headless/HeadlessRenderService.cs:19-81, 110-147, 193-208`,
`OpenUtau.Core/SignalChain/ExportAdapter.cs:13-18`,
`OpenUtau.Core/Render/Renderers.cs:12-91`. `HEADLESS_CLI_RENDERER.md` is older
extraction/design material; it must not override the implemented service.

## 4. YuE2 generation contracts

### Native song-generation interface

| Contract field | Current interface |
| --- | --- |
| Tool / operation | YuE2: style + lyrics, optionally external ABC, to plan or complete audio (**V** help/default parsing; inference **G locally / S in source**). |
| Entry point | Installed `yue2` maps to `yue2.cli:main`; source equivalent `PYTHONPATH=/home/jeffbarnes/YuE/src python3 -B -m yue2.cli`. No installed `yue2` executable was found in this environment. |
| Working directory | YuE root for source templates; arbitrary cwd possible with absolute paths/PYTHONPATH. Request `abc_path` resolves relative to its request JSON file. CLI `--abc-file`/`--lyrics-file` resolve relative to cwd. |
| Inputs | `--request` JSON, or explicit `--style` and lyrics via `--lyrics` / `--lyrics-file`. `--abc-file` optional. Source API requires style and lyrics strings, which may include empty lyrics. |
| Options | `generate` options printed by verified help: `--model`, `--vae`, `--revision`, `--vae-revision`, `--device`, `--budget`, `--backend`, `--quantization`, `--offload-ar`, `--offline`, `--config`, `--output`, `--cot`, `--resume`, `--quiet`/`--no-progress`, `--request`, `--id`, `--style`, `--lyrics`, `--lyrics-file`, `--abc-file`, `--seed`, `--cfg-scale`, `--stage`. |
| Defaults | Device `auto`, budget 24 GiB, backend `torch`, quantization `none`, stage `audio`; flags off, revisions unset. Request defaults: `cot=full`, seed 831001, ID `song`, CFG 1.0 for full/melody and 1.01 for off. |
| Outputs | `OUTPUT/ID/audio.flac` (default parent `runs/default`), `score.abc` when applicable, plan manifests/tokens, `prefix.npy`, `semantic.npy`, `latent.npy`, `request.json`, `config.json`, `result.json`; plan-only stage saves plan artifacts, no audio. |
| Capability discovery | Top-level/subcommand `--help`; parser/default introspection; source `protocol.py`. `doctor` is available but was not run. No list-all-models CLI. |
| Success criteria | Exit 0, expected nonempty audio/result manifest, `status=complete`, **both truncation flags false**, verified hashes and decodable audio; symbolic score fidelity does not prove audible adherence. |
| Known gaps | No direct MIDI, USTX, Melisma bound-IR, singer, deterministic syllable-binding, or separate vocal/instrumental stem export interface. Current system interpreter lacks inference dependencies. |

Exact help command executed:

```sh
PYTHONPATH=/home/jeffbarnes/YuE/src python3 -B -m yue2.cli generate --help
```

Source-backed generation template, **not executed; requires a separately
validated inference interpreter and models**:

```sh
PYTHONPATH=/home/jeffbarnes/YuE/src "$YUE_PYTHON" -B -m yue2.cli generate \
  --style "$STYLE" --lyrics-file "$SONG_LYRICS" --abc-file "$ABC_INPUT" \
  --cot melody --seed "$SEED" --id "$RUN_ID" --output "$NEW_RUN_PARENT" \
  --model "$MODEL_PATH" --vae "$VAE_PATH" --device cuda --offline
```

`cot` choices are `full`, `melody`, `off`; external nonempty ABC is allowed
only for full/melody. Plain song lyrics are separate text conditioning, not
Melisma's `--`/`_` grammar. Request JSON accepts `style`/`tags`, `lyrics`,
`cot`, `seed`, `abc` or `abc_path`, `cfg_scale`, `id`, `abc_sampling`,
`semantic_sampling` plus documented legacy metadata; conflicting `abc` and
`abc_path` are rejected. CLI fields override corresponding JSON fields.
`--lyrics-file` wins over inline `--lyrics`.

A completely empty request invokes a built-in Mandarin piano-pop sample, ID
`first_song`. **Partial requests do not inherit that sample's missing style
or lyrics**. `--cot`/`--seed`/`--id` parser defaults are unset and obtain
values from JSON or `SongRequest`, not necessarily the sample request.

Models default to `$YUE2_KIT/models/YuE2-3B` and the selected local VAE if
those directories exist; otherwise `m-a-p/YuE2-3B` and
`m-a-p/YuE2-Vae`. Unset `YUE2_KIT` uses the checkout/package-derived kit
root. `--vae standard|legacy` selects standard or `YuE2-Vae-legacy`, and
arbitrary local path/Hugging Face ID is accepted. The safe defaults query
resolved to the two remote standard IDs here, **not installed weights**.
`--offline` prevents fetching missing model files. Without it, first use can
download models. Model identities are content hashes; normal pipeline
construction hashes weight files and is not cheap capability discovery.

`--backend` choices: `torch`, `torch-eager`, `vllm`; `--quantization`:
`none`, `fp8`. Their presence does not establish hardware/runtime compatibility.
Auto device resolves CUDA, else MPS, else CPU in source; CPU availability
does not imply practical Phase 1 generation performance. The documented
unquantized runtime target is Linux, Python 3.12, BF16-capable NVIDIA GPU,
24 GB-class VRAM. The checkout pins Torch 2.10.0, Transformers 4.57.6,
Hugging Face Hub 0.36.2, Safetensors 0.7.0, Tiktoken 0.12.0, NumPy 2.2.6,
SoundFile 0.13.1, Accelerate 1.13.0; optional fast backend dependencies are
separate. No GPU probe or inference was performed.

Source/protocol defaults, also confirmed by safe object introspection:

| Generation setting | ABC planning | Semantic generation |
| --- | ---: | ---: |
| Temperature | 0.7 | 1.0 |
| Top-p / top-k | 0.9 / 30 | 0.95 / 100 |
| Repetition penalty / window | 1.005 / 100 | 1.2 / 50 |
| Minimum / maximum tokens | 32 / 4096 | 200 / 9000 |

Flow matching defaults: 32 ODE steps, `midpoint`, context 24576, protocol
`yue2-native-v1`. `--config` supplies `GenerationConfig` JSON; it is not an
arbitrary model configuration override. Standalone checkpoint
`yue2_generation_config.json` should not automatically be assumed effective:
current constructor defaults to `GenerationConfig()`, unless explicit config
or saved `pipeline.json` metadata supplies one.

Native `generate` returns 0 even for truncated results; check `result.json`.
Argument-parser errors exit 2. Unhandled generation exceptions fail the
process and write `failure.json` only after entering the generation block;
early model/request/path failures need not produce a failure artifact.
Nonempty output is refused unless `--resume` verifies existing result
identity/hashes. The resume path still constructs a pipeline and inspects
weights; it is not a lightweight "list past runs" operation.

Evidence: `YuE/pyproject.toml`, `src/yue2/cli.py:16-153, 205-267`,
`protocol.py:28-124`, `pipeline.py:28-40, 93-117, 122-190, 225-233, 398`,
`storage.py:44-52, 111-153`.

### Existing example entry point

**S generation, V help:** `python3 -B /home/jeffbarnes/YuE/examples/generate.py --help`.
Cwd YuE root for relative paths. Required `--output`; optional `--request`
(default sibling `song.json`), `--abc-file`, `--cot`, `--model`, `--vae`,
`--revision`, `--vae-revision`. Model defaults are the remote standard IDs,
device is hard-coded `cuda`, and output is exactly `OUTPUT/audio.flac`,
**without native CLI's `/ID` suffix**. It refuses any existing output path
and returns 1 when any truncation flag is true. It has no `--offline`,
`--device`, `--seed`, or `--style` flags; those request fields belong in JSON.
Do not combine this interface's options/output naming with the native CLI.

Template (not run):

```sh
"$YUE_PYTHON" -B /home/jeffbarnes/YuE/examples/generate.py \
  --request "$REQUEST_JSON" --abc-file "$ABC_INPUT" \
  --cot melody --output "$NEW_EXAMPLE_OUTPUT" \
  --model "$MODEL_PATH" --vae "$VAE_PATH"
```

### Melisma's current instrumental path

The prototype does **not** invoke the native CLI directly. It uses the
existing skill helper:
`skills/yue2-music/instrumental/scripts/instrumental.py`.
This is a concrete supported source interface, not a hypothetical `yue2
--instrumental` switch.

| Contract field | Current interface |
| --- | --- |
| Tool / operation | YuE2 helper: transform Melisma ABC into instrumental request, generate audio, verify handoff (**V** help; transforms/files/inference **S**). |
| Entry point / working directory | Standard-library Python for provided-score preparation; pinned inference interpreter for generation; cwd `/home/jeffbarnes/YuE` for relative script/model paths. |
| Inputs | `cover --abc ABC` or audio reference (separate transcriber required); `generate --prepared DIR`; `verify DIR`. This path intentionally does **not** sing Melisma lyric words. |
| Options | Cover: `--composer model|agent`, style or style file, `--empty-lyrics`, ID/seed/output, `--prepare-only`, overlap policy, optional `--keep-harmony`, runtime/transcriber options shown by help. Generate: required `--prepared`, `--output`; optional `--models-root`, `--device`, `--offline`. |
| Defaults | Composer `model`, plan mode `full`, overlap priority `vocal`, ID `instrumental`, seed 831001, device `cuda:0`; final mode is inferred from score chords, not plan-mode. Prepared lyrics are section tags unless explicitly empty. |
| Outputs | Cover root contains `conversion/`, `prepared/`, `workflow.json`; without prepare-only also generation/listening handoff. Separate generate root contains `native/audio.flac`, native manifests/arrays, copied score/prompt, `summary.json`, `delivery-manifest.json`, optional MP3/player. |
| Capability discovery | `cover --help`, `generate --help`, helper parser, release manifest, `setup_runtime.py doctor` (available, not run). |
| Success criteria | Preparation exit 0 and valid `prepared-manifest.json`; generation exit 0 with complete summary/native audio and no truncation; `verify` exit 0 establishes file/symbolic integrity, **not absence of vocals or audible timing fidelity**. |
| Known gaps | No installed local pinned interpreter/models found; no automatic lyric-note alignment or stem separation; generation quality requires listening. Version pin conflicts with checkout package version. |

Exact source-backed local equivalent of the prototype's remote handoff,
**not run**:

```sh
python3 -B skills/yue2-music/instrumental/scripts/abc_tools.py inspect "$ABC_INPUT"
python3 -B skills/yue2-music/instrumental/scripts/instrumental.py cover \
  --abc "$ABC_INPUT" --composer agent --style "$INSTRUMENTAL_STYLE" \
  --empty-lyrics --id "$RUN_ID" --seed "$SEED" \
  --output "$NEW_PREPARATION_ROOT" --prepare-only --offline
"$YUE_INSTRUMENTAL_PYTHON" -B skills/yue2-music/instrumental/scripts/instrumental.py \
  generate --prepared "$NEW_PREPARATION_ROOT/prepared" \
  --output "$NEW_GENERATION_ROOT" --models-root "$PINNED_MODELS_ROOT" \
  --device cuda --offline
"$YUE_INSTRUMENTAL_PYTHON" -B skills/yue2-music/instrumental/scripts/instrumental.py \
  verify "$NEW_GENERATION_ROOT"
```

These are separate operations, not a new script. Each output root must be new.
`--prepare-only` is CPU-only **when a score is supplied**; a request that
first asks the model to compose still needs a GPU.

Melisma ABC's notes begin in `Vocal`, with `Ins` rests. `cover` uses
`instrumentalize.convert_score()` to move sounding notes to `Ins`, preserve
pitch/onset/duration/meter/tempo, and rest the vocal voice. Overlapping
instrumental events have an explicit vocal-priority/error policy.
Direct `prepare` on unchanged Melisma ABC would reject sounding `Vocal`
notes; the conversion step is necessary. Style text gains an `Instrumental`
prefix and no-vocals/no-singing/no-choir/no-spoken-words conditions.
No chords means final `cot=melody`; chords imply `full`.

**Important version/runtime gap:** `instrumental/assets/release.json` and
`requirements-inference.txt` pin **`yue2-infer==0.1.5`**, unlike checkout
`pyproject.toml`/`src/yue2/__init__.py` **0.1.6**. `open_pipeline()` explicitly
checks that package version and frozen model hashes. Installing the current
checkout into that environment does not satisfy this recipe. Its model
revisions are:

| Pinned recipe model | Revision |
| --- | --- |
| `m-a-p/YuE2-3B` | `14fc6c6f146441b1dd6363fcb2e01e82a6914cb7` |
| `m-a-p/YuE2-Vae` | `9a94e1d0ea9f8087e98f77fa88df4a4068104d2a` |

`--models-root` must contain `YuE2-3B` and `YuE2-Vae`. The helper requires
CUDA with compute capability >=8, uses a 24 GiB budget, and verifies pinned
assets. `setup_runtime.py doctor` checks versions/GPU and optionally hashes
models; no-output form avoids writing reports but still imports Torch/queries
hardware. `models` and `install` write/download and are **not discovery-only**.
None was run.

Helper exit codes: 0 complete; generation returns 1 for truncation/needs-review;
handled input/runtime/integrity exceptions and parse errors return 2.
Failure artifacts may be retained. Its generation wrapper optionally writes
an MP3/listening HTML using ffmpeg; Phase 1 must not assume these are primary
audio or implement a new UI here.

Evidence: `instrumental/scripts/instrumental.py:23-169, 188-280, 285-319,
396-509`, `instrumentalize.py:47-118`, `setup_runtime.py:39-127`,
`instrumental/assets/release.json`.

### Experimental Gloria lyric/slur path

`YuE/tools/native_slur_experiment.py` provides `validate` and `run`.
`validate` was executed safely and returned `status=valid`, without model
loading or files. It tests fixed three-/five-note ABC conditions with and
without literal standard slurs, using lyrics `Glo ri a`, 2/4, 88 BPM,
`L:1/16`, and `cot=melody`. This is **not the same timing** as Melisma's
current five-note fixture.

Verified command:

```sh
PYTHONPATH=/home/jeffbarnes/YuE/src python3 -B \
  /home/jeffbarnes/YuE/tools/native_slur_experiment.py validate
```

Source-backed experimental generation template (not run):

```sh
PYTHONPATH=/home/jeffbarnes/YuE/src "$YUE_PYTHON" -B \
  /home/jeffbarnes/YuE/tools/native_slur_experiment.py run \
  --output "$NEW_EXPERIMENT_DIR" --model "$MODEL_PATH" --vae "$VAE_PATH" \
  --revision "$MODEL_REVISION" --vae-revision "$VAE_REVISION" \
  --device cuda --offline
```

Both revision arguments are required. Defaults include seed 831001 in fixed
requests, budget 24, backend torch, quantization none, CFG 1.0; optional
offload/quiet flags exist. It produces per-condition native artifacts and
experiment provenance. It accepts no arbitrary Melisma MIDI/lyric input.
The source explicitly disclaims deterministic lyric alignment and requires
listening to interpret syllable semantics. Melisma ABC currently exports no
such lyric-dependent slurs. A standalone successful `validate` does not prove
the Melisma-to-YuE2 sung-lyric requirement.
Evidence: `tools/native_slur_experiment.py:1-63, 536-585`.

## 5. ffmpeg mixing contract

| Contract field | Current interface |
| --- | --- |
| Tool / operation | `/usr/bin/ffmpeg` `6.1.1-3ubuntu5`: gain-stage and mix instrumental plus vocal audio (**V** to null; persistent file output **S**). |
| Working directory | Any with absolute paths; verification used Melisma root and relative reference files. |
| Inputs | Input 0 instrumental/accompaniment audio; input 1 OpenUtau vocal audio. Both must decode and be intentionally timeline-aligned. |
| Options | Two `-i` paths, `-filter_complex` graph, explicit `-map "[out]"`, `-c:a pcm_s16le`, `.wav` output. `-nostdin` prevents parent stdin interaction; `-n` refuses overwrite. |
| Defaults | Filter `volume=1.0`; native `amix` defaults inputs 2, duration **longest**, normalize **true**, weights `1 1`, dropout transition 2 seconds. These differ from prototype policy. |
| Prototype policy | Instrumental gain 1.0, vocal 0.5, master 0.707945784; `duration=first`, `normalize=0`. This is a Melisma preset, not ffmpeg's default. |
| Outputs | Explicit mixed PCM 16-bit WAV; no model generation or stem extraction. Parent directory must exist. |
| Capability discovery | `-version`, `-h filter=amix`, `-h filter=volume`, `-h encoder=pcm_s16le`, relevant `-h full` options were inspected. |
| Success criteria | Exit 0, fresh nonempty decodable audio, intended duration/channel policy, no unexpected clipping. Use ffprobe plus decoded-audio/peak assessment; file existence alone is insufficient. |
| Known gaps | Mixing neither removes YuE2 vocals nor corrects tempo/onset drift; no guaranteed limiter/normalization. Different durations require an explicit policy. |

Verified null-output command, exit 0:

```sh
/usr/bin/ffmpeg -hide_banner -nostdin -v error \
  -i reference/yue2-diffsinger-gloria/piano-opening-4s.wav \
  -i reference/yue2-diffsinger-gloria/gloria-diffsinger-clean_Track1_Track1.wav \
  -filter_complex '[0:a]volume=1.0[accompaniment];[1:a]volume=0.5[vocal];[accompaniment][vocal]amix=inputs=2:duration=first:normalize=0[mix];[mix]volume=0.707945784[out]' \
  -map '[out]' -c:a pcm_s16le -f null -
```

This successfully combined 48 kHz stereo instrumental and 44.1 kHz mono
vocal inputs using ffmpeg's automatic format/rate negotiation; it discarded
audio rather than writing a file. It did not assess listening quality.

Minimum persistent-output template (**not run**):

```sh
/usr/bin/ffmpeg -hide_banner -nostdin -n \
  -i "$INSTRUMENTAL_AUDIO" -i "$VOCAL_AUDIO" \
  -filter_complex "[0:a]volume=${INSTRUMENTAL_GAIN}[accompaniment];[1:a]volume=${VOCAL_GAIN}[vocal];[accompaniment][vocal]amix=inputs=2:duration=first:normalize=0[mix];[mix]volume=${MASTER_GAIN}[out]" \
  -map '[out]' -c:a pcm_s16le "$NEW_MIX_WAV"
```

Validate gains as numeric values before construction; do not accept arbitrary
filter expressions from basic gain fields. Linear 1.0 means unchanged, 0.5
is approximately -6 dB; neither is peak normalization. The existing Python
prototype requires positive finite gains (no zero mute gain), uses `-y`
internally in its managed run workspace, and rejects a measured final
`max_volume >= 0.0 dBFS`. The future adapter should expose any intentional
deviation, not silently use `amix`'s normalization default.

Source-backed checks for a future produced artifact:

```sh
/usr/bin/ffprobe -v error -select_streams a:0 \
  -show_entries stream=codec_name,sample_rate,channels:format=duration \
  -of json "$MIX_WAV"
/usr/bin/ffmpeg -hide_banner -nostdin -nostats -i "$MIX_WAV" \
  -af volumedetect -f null -
```

These templates read the output, with peak diagnostics on stderr; they do
not create another audio file. The null-mix test verified syntax/processing,
not persistent WAV creation or safe peak levels of the resulting mix.
Evidence: installed filter/encoder help and
`Melisma/tools/melisma_pipeline.py:425-495`.

## 6. ffplay playback and parent-process contract

| Contract field | Current interface |
| --- | --- |
| Tool / operation | `/usr/bin/ffplay` `6.1.1-3ubuntu5`: play selected audio and stop own child process (**V**, using SDL dummy audio). |
| Working directory | Any; use an absolute selected artifact path. |
| Inputs | One decodable local audio artifact and a working SDL audio environment for audible playback. |
| Options | `-nodisp` disables window, `-autoexit` exits at EOF, `-vn` disables video, `-i PATH` selects input; `-hide_banner`, `-loglevel error` control diagnostics. `-volume` startup volume exists, but is not the mix gain contract. |
| Defaults | Without `-autoexit`, EOF is not a reliable parent completion policy. Specify headless behavior instead of inheriting graphical defaults. |
| Outputs | Playback only; no generated file. stdout/stderr and process state are stage records. |
| Discovery | `-version`, relevant installed `-h full` options. No singer/model discovery applies. |
| Success criteria | Validated input plus process startup/live state and no decoder/device errors; EOF completion distinguished from parent-requested stop. Exit 0 alone is insufficient. |
| Known gaps | Audible playback/device behavior not tested; process exit codes do not reliably indicate input errors. No harness process manager exists yet. |

Exact playback argument vector exercised by the verification driver:

```sh
/usr/bin/ffplay -hide_banner -loglevel error -nodisp -autoexit -vn \
  -i /home/jeffbarnes/melisma/reference/yue2-diffsinger-gloria/piano-opening-4s.wav
```

Verification set `SDL_AUDIODRIVER=dummy` and `SDL_VIDEODRIVER=dummy` **only
in the child environment** and connected stdin to `/dev/null`. Natural EOF
returned 0 without error. A second child was alive after 0.4 seconds; parent
`Popen.terminate()` sent SIGTERM to that specific PID, and it exited **123**
within the five-second wait. Do not assume stop must return 0 or -15.

**A missing input returned 0** while stderr reported
`No such file or directory`. Therefore preflight the file, capture stderr,
and distinguish error/EOF/stopped states rather than treating any zero exit
as successful playback.

Recommended parent contract: retain the exact child handle/PID; spawn without
a shell, close stdin, capture logs; stop that child with SIGTERM; wait/reap
with a bounded timeout; only escalate to SIGKILL on that same child if needed.
Record parent-requested stop independently of numeric return code.
No name-based killing or detached playback is necessary. The test did not
need escalation. `-nostdin` was not listed as an ffplay option and is not
included; stdin control belongs to the parent here.

## 7. Gloria/five-notes reference artifact inventory

Inventory is of actual local files, not expected future pipeline outputs.
The complete Melisma audio/ABC/USTX/JSONL glob found only the reference USTX,
four reference WAVs, and the standalone TIGER WAV listed below. No stored
`.abc`, `.flac`, or `bound.jsonl` was found in this checkout.

| Actual artifact (relative to Melisma root) | Role and inspected properties |
| --- | --- |
| `examples/five-notes.mid` | Tracked source input/regression fixture. Runtime import verified: PPQN 480; C4/D4/E4/F4/G4, each 480 ticks; last note endpoint 2400 ticks. |
| `examples/gloria.txt` | Tracked source lyric fixture: `Glo - ri - a -- --`. Runtime binding verified. |
| `reference/yue2-diffsinger-gloria/first.ustx` | Untracked saved reference project, suitable as a candidate OpenUtau input, not proven byte-equivalent to current serializer. Saved TIGER/English+ /DIFFSINGER selections; PPQN 480, BPM 120, five pitches 60..67, **960 ticks each** at 0/960/1920/2880/3840, part duration 7680. |
| `reference/yue2-diffsinger-gloria/gloria-diffsinger-clean_Track1_Track1.wav` | Existing named vocal-reference/generated-output candidate: PCM s16le, 44,100 Hz **mono**, 2.609615 seconds, 230214 bytes. Naming/mono format must not be mistaken for headless CLI's current stereo single mixdown contract. |
| `reference/yue2-diffsinger-gloria/piano-opening-4s.wav` | Existing piano/instrumental-reference candidate: PCM s24le, 48,000 Hz stereo, 4.000000 seconds, 1152102 bytes. Filename/folder suggest YuE2 experiment use; no generation receipt proves model/provenance. |
| `reference/yue2-diffsinger-gloria/melisma-end-to-end-gloria.wav` | Existing mixed-reference candidate: PCM s24le, 48,000 Hz stereo, 4.000000 seconds, 1152102 bytes. |
| `reference/yue2-diffsinger-gloria/melisma-gloria-mix-piano-forward.wav` | Existing alternate-name mixed-reference candidate with the same format/duration/size. **Byte-identical** to the preceding WAV; not an independent mix comparison. |
| `tiger-a-c4.wav` | Preserved untracked standalone test output: PCM s16le, 44,100 Hz mono, 0.271519 seconds, 23994 bytes. Name does not prove singer/model reproducibility. |
| `binder.test` | Preserved untracked lyric text equal to the simple Gloria phrase; no automated-fixture role established. |
| `lyric.test` | Preserved untracked collection of lyric syntax probes, including deliberately invalid `---`; do not pass the entire file as a known-good one-phrase fixture. |
| `note.txt` | Preserved untracked saved USTX command selecting TIGER/English+/DIFFSINGER. Command evidence only, not an existing `~/gloria-tiger.ustx` artifact verified here. |
| `tests/cli_test.c`, `tests/render_inputs_cli_test.c` | Tracked regression-fixture definitions containing expected IR/ABC and USTX assertions. They produce temporary test artifacts when run; tests were read, not executed. |
| `examples/intro-vocal-interlude.mid`, `examples/intro-vocal-interlude.txt`, `examples/light-in-the-ruins.mid`, `examples/light-in-the-ruins.txt` | Additional tracked input pairs, not Gloria output/reference replacements. Timing acceptance documentation describes opt-in costly rendering; not run. |

For the two identical mix files, SHA-256 is
`c938a91321f42edbc22edddc844ab670b83ed6936d282bedbba4e1c8aef6ac44`.
The hash comparison was read-only.

**Timing mismatch:** `first.ustx`'s doubled note durations imply a last-note
end of 5.0 seconds, and its part extent 8.0 seconds at 120 BPM. Current
Melisma emits notes ending at 2.5 seconds and ABC padded to 4.0 seconds.
The saved vocal audio is 2.609615 seconds. These files cannot be silently
declared one sample-aligned, regenerated reference set.

**Missing references:** no Melisma-root `first.ustx`; the actual one is nested
under `reference/yue2-diffsinger-gloria/`. No saved Gloria ABC, bound IR,
current `vocal.ustx`, `vocal.wav`, pipeline manifest, native YuE2 FLAC/result,
prepared score/request, model-identity receipt, or experimental condition
audio exists in the inspected Melisma artifact inventory.
YuE's `examples/*.abc` and experimental ABC strings exist in the dependency
checkout, but are not Melisma-generated Gloria references.

The user-provided folder is a collection of **candidate known-good listening
references**. Only file integrity/headers and symbolic fixtures were checked;
no subjective listening, origin reconstruction, hash-against-generation
comparison, or regeneration occurred. Preserve them unchanged.

## 8. Capability-discovery matrix

| Capability | Safe mechanism available now | Verified result / limitation |
| --- | --- | --- |
| Melisma operations/formats | Usage (no args or invalid help); source enum/parser | V usage: single IR/ABC/USTX and `render-inputs`. No MIDI producer/schema API. |
| Melisma lyric/note model | Lexer/grammar/importer/tests; stdout serialization | V five-note binding. Textual note generation and general lyric input absent. |
| Melisma pipeline options | `python3 -B tools/melisma_pipeline.py --help` | V argparse help. Running preflight writes state and contacts SSH; not discovery-only. |
| OpenUtau CLI syntax | Built executable `--help` | V only render and help; no listing/selection flags. |
| Installed singers/TIGER | Source-defined external paths + explicitly authorized read-only metadata inspection in future | G here: external runtime contents not inspected, installed TIGER unknown. Never invoke initializing managers for safe discovery. |
| Phonemizer candidates | Source classes/attributes and plugin paths/assemblies | S English+ class exists; installed/loadable external plugin set unknown. Reflection/loading through DocManager has side effects. |
| Renderer candidates | Source `GetSupportedRenderers` and singer type | S typed lists available, no CLI enumeration; does not prove backend assets ready. |
| OpenUtau defaults | USTX fields + core source; external preferences only if authorized | Source defaults known; effective installed preferences unknown. |
| YuE2 generation options | Native `generate --help`; parser choices | V choices/help work without inference packages. |
| YuE2 models | `model_paths`, optional local kit-directory metadata, pinned recipe release JSON | V resolves remote defaults here; S arbitrary paths/IDs. No installed-model list or weight-validating lightweight query. |
| YuE2 environment | Python distribution metadata; source `doctor` | V active runtime missing dependencies. Doctor is S and imports Torch/queries hardware; do not use `--verify-hashes` as cheap discovery. |
| Instrumental helper | `cover --help`, `generate --help`, source parser/release | V help. Pinned package 0.1.5 is a separate interface/environment requirement. |
| ABC diagnostics | `abc_tools.py inspect PATH` without `--output` | S read-only structural validation/report to stdout; cannot check audio alignment or quality. |
| Slur experiment | `native_slur_experiment.py validate` | V fixed conditions valid; no deterministic lyric-attachment claim. |
| ffmpeg Phase 1 filters/encoder | Installed `-h filter=amix`, `-h filter=volume`, `-h encoder=pcm_s16le` | V available; mixed differing audio formats to null successfully. |
| ffplay playback/process | Installed help; short dummy-driver child execution | V EOF, targeted SIGTERM, misleading missing-input exit. Actual audio device remains untested. |

## 9. Default-discovery matrix

| Setting | Actual default and provenance | Resolution recommendation for a future adapter |
| --- | --- | --- |
| Melisma output mode/path | Tool source: `ir`, stdout if omitted | Preserve/report tool default; request an explicit workspace output when retaining artifacts. |
| Melisma tempo/meter/key | Serializer source: 120 BPM, 4/4; ABC C | Fixed behavior, not user-discovered MIDI metadata or configurable flags. |
| Melisma USTX singer/class/renderer | Tool source: empty strings | Require validated selections for vocal rendering. Reference preset is not installation discovery. |
| OpenUtau input/output | CLI has no defaults; both mandatory | Upstream selected USTX plus new explicit WAV destination. |
| OpenUtau singer | Serialized singer ID; no verified automatic installed choice | Installed discovery deferred; manual/reference value must be labeled unvalidated. |
| OpenUtau phonemizer | Saved class; fallback `DefaultPhonemizer` | Check intended class, do not present fallback as English+ detection. |
| OpenUtau renderer | Saved setting, else source singer-type default; Classic preference affects Classic selection | Infer only from validated singer type; installed effective settings unknown. |
| OpenUtau inference parameters | Source defaults; external `prefs.json` may override | Show source provenance and mark effective external values unknown. |
| YuE2 model/VAE | Kit-local directories if present, otherwise remote IDs; V here remote standard pair | Read configuration/metadata without weight loading; require verified explicit environment/model paths for offline runs. |
| YuE2 seed/COT/CFG | `SongRequest`: 831001/full/1.0; off CFG 1.01 | Separate CLI unset defaults from JSON and protocol effective defaults. |
| YuE2 output | Native parent `runs/default`, then request ID; example output no ID suffix | Entry-point-specific artifact naming, not a universal YuE output path rule. |
| YuE2 device/backend | auto/torch/none/24 GiB from parser/source | Device auto is hardware-dependent; mark actual readiness separately. |
| YuE2 sampling | `GenerationConfig` values in section 4 | Prefer safe source/protocol query; persist effective config with result, not assumptions from arbitrary checkpoint files. |
| Instrumental helper | Pinned release 0.1.5; ID instrumental, seed 831001, CUDA:0; score-dependent COT | Distinct recipe preset/environment, not native CLI defaults. |
| Prototype pipeline tool roots | Local Melisma/OpenUtau; remote `/workspace/YuE` | Do not use remote default as the configured local checkout. |
| Mixing gains/duration | ffmpeg volume 1.0, amix longest/normalized; prototype 1.0/0.5/0.707945784 and first/non-normalized | Explicit provenance: tool default versus Melisma project preset. Keep timeline policy visible. |
| Playback | Selected artifact; explicit nodisp/autoexit/vn policy | No persistent audible-device default verified; dummy drivers are verification-only, not user playback defaults. |

Previous user selection, most recent upstream artifact, workspace naming, and
fallback values would be **harness policies**, not discovered tool defaults.
Issue #2 should keep their provenance separate and revalidate saved choices.

## 10. Known interface and automation gaps

| Gap | Consequence for Phase 1 |
| --- | --- |
| Melisma consumes but does not generate MIDI | Disable/label "Generate MIDI" unavailable; permit existing MIDI selection/import instead. |
| No free-form note-input or project-editing CLI | Do not manufacture a note-editor-to-MIDI command. |
| Fixed serializer tempo/meter and ignored MIDI tempo metadata | Do not imply exact reproduction of arbitrary MIDI tempo maps. |
| ABC omits lyric bindings and starts with Vocal melody/Ins rests | Use existing instrumental conversion for accompaniment; deterministic sung-lyric alignment is not implemented. |
| OpenUtau CLI lacks resource/default enumeration and override flags | Configure USTX and separately validate resources; no invented `--singer` render argument. |
| OpenUtau external config is necessary but uninspected | TIGER readiness/location/effective preferences unknown. Honor access boundaries; never silently report installed discovery. |
| OpenUtau initialization can write/delete files | Help is safe; invoking managers/DocManager is not read-only discovery. |
| Native YuE2 runtime unavailable in active Python environment | Help/default validation succeeds without making generation ready. Dependency/model provisioning is future explicit work. |
| Instrumental recipe pins 0.1.5 while checkout declares 0.1.6 | Separate environments/contracts; do not silently remove pin checks or claim compatibility. |
| Model enumeration/validation not unified | Metadata candidates, remote IDs, hash-verified weights, and GPU readiness are different states. |
| Prototype orchestration is remote-specific | It cannot be used as an unmodified local full-pipeline command; no local mode/playback UI exists. |
| Prototype does not retain every successful command's logs | Future process/artifact layer must capture persistent commands/stdout/stderr itself. |
| YuE2 native exit 0 may include truncation | Inspect truncation and artifact receipts; stage success is not merely process success. |
| No vocal-free guarantee, stems, or exact audible timing from YuE2 | Structural conversion/verification cannot certify listening quality; alignment/conformance requires explicit evidence. |
| Reference timing/provenance mismatch; duplicate named mixes | References are not a fully reproducible golden end-to-end run. |
| ffplay missing-file exit 0 | Preflight input, retain errors, and distinguish stopped/EOF/failed states. |
| Process cancellation varies by tool | ffplay targeted stop verified; OpenUtau executable does not expose service cancellation; expensive generation cancellation untested. |

**Deferred verification:** bundle/file creation and persistent mix output,
OpenUtau vocal render/TIGER resolution, YuE2 model readiness/download/hash
verification, GPU generation, remote SSH/SCP pipeline, audible playback, and
audio-quality/lyric-alignment judgments. No full pipeline was run. No
dependency checkout or existing untracked artifact was modified.

## 11. Recommended adapter boundaries for Issue #2

These are documentation recommendations only; Issue #2 was not started.

| Boundary | Responsibility and constraints |
| --- | --- |
| Melisma adapter | Import MIDI + Melisma lyric grammar, produce IR/ABC/USTX or bundle, exact exit handling, output protection; report MIDI generation unsupported. Keep serializer limits separate from runtime resource selection. |
| OpenUtau adapter | One strict render argv, explicit cwd/output, USTX metadata/resource checks, .NET/binary identity, WAV validation. Expose safe help and unknown installed resources; never call side-effecting discovery APIs during a refresh. |
| YuE2 adapter | Explicitly select native CLI versus pinned instrumental helper. Keep provided-score conversion/preparation, expensive generation, and verification independently rerunnable. Model/runtime/version provenance and truncation are mandatory. No inferred MIDI/USTX or bound-IR input. |
| ffmpeg adapter | Two explicit audio inputs, numeric gains, visible duration/normalization policy, non-overwrite workspace output and post-mix checks. Keep optional timing conformance outside the minimal mix operation. |
| ffplay adapter | Input validation, exact own child handle/PID, stderr capture, EOF/error/stopped state, bounded targeted termination/reaping. Never rely on zero exit alone or kill by name. |
| Common process/artifact layer | Structured argv/cwd, separate stdout/stderr, exit/signal/cancel intent, expected-artifact verification and preservation. New run paths outside dependency checkouts; no source edits/build/install hidden inside capability queries. |
| Capability/default records | Track value, source/provenance, verification level, timestamp/revision, and unknown/error states. Never flatten source support into installed readiness. |

The existing Python pipeline offers prior art for command construction,
manifest/hash checks and failure retention, but its remote assumptions,
overwrite policy, recipe dependencies and timing choices must not leak into
the local adapter contract as presumed defaults.

## 12. Safe verification record and Issue #1 acceptance

Read-only source/directory/glob inspection covered the three checkouts,
requirements/checklist, examples, tests, reference project, and preserved
lyric/command notes. Git status/revision and `gh issue view 1
--json title,body,state,url` were read; no issue/PR updates or commits occurred.
Initial tool-path/version checks resolved ffmpeg/ffplay/ffprobe/.NET/Python
and read `ffmpeg -version`, `ffplay -version`.

Successful safe CLI checks, with commands shown individually to the user
before subsequent shell executions:

```sh
./build/melisma --help
./build/melisma --version
./build/melisma examples/five-notes.mid examples/gloria.txt
./build/melisma --format abc examples/five-notes.mid examples/gloria.txt
./build/melisma --format ustx --singer TIGER_DS_v102 --phonemizer OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer --renderer DIFFSINGER examples/five-notes.mid examples/gloria.txt
python3 -B tools/melisma_pipeline.py --help
/home/jeffbarnes/OpenUtau-CLI/OpenUtau.Cli/bin/Release/net8.0/openutau-cli --help
dotnet --list-runtimes
PYTHONPATH=/home/jeffbarnes/YuE/src python3 -B -m yue2.cli --help
PYTHONPATH=/home/jeffbarnes/YuE/src python3 -B -m yue2.cli generate --help
python3 -B /home/jeffbarnes/YuE/examples/generate.py --help
python3 -B /home/jeffbarnes/YuE/skills/yue2-music/instrumental/scripts/instrumental.py cover --help
python3 -B /home/jeffbarnes/YuE/skills/yue2-music/instrumental/scripts/instrumental.py generate --help
PYTHONPATH=/home/jeffbarnes/YuE/src python3 -B /home/jeffbarnes/YuE/tools/native_slur_experiment.py validate
/usr/bin/ffmpeg -hide_banner -h filter=amix
/usr/bin/ffmpeg -hide_banner -h filter=volume
/usr/bin/ffmpeg -hide_banner -h encoder=pcm_s16le
```

Melisma help/version exit 2 is the observed unsupported-flag result, not a
successful special command. Other listed help/default/validation queries
returned 0.

Additional read-only checks:

- Installed ffmpeg/ffplay full help filtered to relevant options.
- `ffprobe -v error -show_entries
  format=duration,size:stream=codec_name,sample_rate,channels,bits_per_sample
  -of json PATH`, separately for all five WAVs in section 7.
- The exact two-input ffmpeg null-mix command in section 5.
- Python `-B` metadata query of active package distributions/PATH/environment;
  no Torch import, GPU query, or installation.
- Python `-B` source query importing `yue2.cli.parser/model_paths` and
  `yue2.protocol.GenerationConfig/SongRequest` to print their defaults;
  no weights, network, or files created.
- Python `-B` subprocess driver for section 6: dummy SDL environment,
  `/dev/null` stdin, natural EOF (10-second timeout), second child SIGTERM
  after 0.4 seconds (five-second timeout), and absent-input probe. Only
  child handles owned by that driver were used.
- `sha256sum` of all twelve explicitly preserved files before documentation
  creation, including every file under the supplied reference directory.

Final `sha256sum --check` confirmed all twelve preserved files unchanged.
Git status confirmed this document is the only new Melisma change, there are
no tracked-file diffs, OpenUtau still has only its pre-existing untracked
`console_out.txt`, and YuE remains clean. The document's
`git diff --no-index --check /dev/null docs/phase1-workflow-contract.md`
reported no whitespace diagnostics (exit 1 reflects the new-file difference).

Commands initially blocked before execution were not counted as verification;
filter-help and WAV probes were subsequently retried successfully. External
OpenUtau runtime-data access was not performed.

| Actual GitHub Issue #1 acceptance criterion | Outcome |
| --- | --- |
| No required command-line options based on assumptions | Satisfied: flags/order come from installed help, executed commands or current parsers/source; templates and execution limits are labeled. |
| Capability/default discovery mechanisms documented | Satisfied: sections 8-9 distinguish safe help/source inspection, unsupported enumeration, side effects, and deferred external discovery. |
| At least one concrete command contract for every Phase 1 tool | Satisfied: Melisma, OpenUtau-CLI, YuE2, ffmpeg and ffplay each have commands/templates and artifact/exit contracts. |
| Gloria/five-notes reference inputs identified | Satisfied: actual input files, reference inventory, timing differences, missing artifacts and duplicate mixes are explicit. |

**All discovery/documentation acceptance criteria are satisfied.** This does
not assert that all conceptual Phase 1 workflows are currently supported or
that the full audio pipeline is ready: the unsupported/deferred operations
above are part of the contract, not silently filled assumptions.
