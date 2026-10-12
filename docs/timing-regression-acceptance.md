# Instrumental timing acceptance

The automated CTest regression uses
`examples/intro-vocal-interlude.mid` and
`examples/intro-vocal-interlude.txt`. It proves that the first sung note is at
tick 1920 in both the bound IR and USTX while the complete twelve-note score is
preserved in ABC.

## Headless DiffSinger

This check is opt-in because it requires the `TIGER_DS_v102` voicebank and a
built OpenUtau CLI checkout:

```sh
python3 tools/accept_diffsinger_timing.py \
  --output /tmp/intro-vocal-interlude-diffsinger \
  --melisma ./build/melisma \
  --openutau-project ~/OpenUtau-CLI/OpenUtau.Cli/OpenUtau.Cli.csproj
```

The script creates the render-input bundle, renders `vocal.ustx`, verifies the
WAV with `ffprobe`, requires the WAV to extend beyond the final vocal-note
endpoint at 4.0 seconds, and prints `volumedetect` measurements for the
instrumental-intro region (0.0-2.0 seconds) and vocal region (2.0-4.0 seconds).
It intentionally establishes no permanent RMS threshold.

## YuE2 pipeline

Run this only as an explicit GPU acceptance test with the current RunPod
endpoint:

```sh
python3 tools/melisma_pipeline.py \
  --midi examples/intro-vocal-interlude.mid \
  --lyrics examples/intro-vocal-interlude.txt \
  --output /tmp/intro-vocal-interlude-pipeline \
  --singer TIGER_DS_v102 \
  --phonemizer OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer \
  --renderer DIFFSINGER \
  --style "Instrumental, simple piano, no vocals, no singing, no choir, no spoken words" \
  --seed 831001 \
  --runpod-host RUNPOD_HOST \
  --runpod-port RUNPOD_PORT \
  --runpod-user root \
  --ssh-key ~/.ssh/id_ed25519
```

Acceptance expectations:

- `melisma/bound.jsonl` contains twelve notes and its first vocal onset is 1920.
- `melisma/vocal.ustx` contains four vocal notes, with its first note at
  absolute tick 1920.
- `melisma/accompaniment.abc` contains all twelve notes over three measures.
- `manifest.json` reports `nominal_score_duration_seconds` as 6.0.
- `final.wav` has a duration of 6.0 seconds, subject only to normal container
  timestamp precision.

The normal test suite does not invoke this command or contact RunPod.

## Prototype mix gains

The Phase 4 pipeline defaults to an accompaniment gain of `1.0`, a vocal gain
of `0.5`, and a post-sum master gain of `0.707945784` (-3 dB). These are
prototype mix defaults rather than normalization. FFmpeg `amix` normalization
remains disabled, and the pipeline runs `volumedetect` on `final.wav`; an
output whose measured peak reaches 0.0 dBFS is rejected as potentially
clipped.
