# Audio specifications and measurement validation

Validated locally on 2026-09-19, on the frontend and backend `dev` branches, including the album completion and flexible review worktree integrations. No merge to `master` or deployment.

## Implemented behavior

- The mastering workflow retains the existing `master` revision, upload, delivery and final-review behavior, alongside the separately integrated flexible review feature. Pre-master confirmation/request/handoff endpoints and permission gates have been removed. Legacy database columns, records and the already-applied migration remain for compatibility.
- Album `audio_specs` and track `audio_spec_overrides` contain independent `source` and `master` specifications. A null track value inherits the album; `enabled: false` disables that category. Responses include effective specifications and structured differences.
- Specifications are advisory. The UI confirms each mismatching upload, delivery confirmation and final approval separately. Cancellation makes no request. Unknown results do not block progress. Only container, sample rate and PCM format participate; loudness is not a delivery requirement.
- Format probing is saved before loudness analysis. The browser reads file headers before uploads; server metadata governs subsequent operations. External links are not applicable.
- Analyzer version 2 stores the FFmpeg version. Sample peaks and DC offsets use full-file `astats`; True Peak uses floating-point resampling to `max(4 × source sample rate, 192000)` and full-output `astats`. All passes select `0:a:0`; decoded sample count determines the selected stream's duration.
- Integrated LUFS is gated on the unpadded source. LRA uses a separate 1.5-second tail. Silence, insufficient duration and no blocks above the absolute gate are explicit states, with null numeric results. LRA below 3 seconds is unavailable; 3–60 seconds carries a short-programme note.
- Older ready analyses are hidden and enqueued on application startup. Failures retry up to three times. Interrupted analyses older than 30 minutes can be retried on startup. Technical information expands on demand and identifies the selected file version.

## Executed checks

FFmpeg: `2023-03-05-git-912ac82a3c-full_build-www.gyan.dev` on Windows.

| Check | Result |
| --- | --- |
| Existing track API suite | 95 passed, including ordinary mastering revision, external stems, upload then confirmation, two-party final review, return/re-delivery, custom workflows and R2 upload API cases |
| Analysis, generated-signal regression and specification suites | 63 passed |
| Track API, analysis and generated-signal suites after integration | 154 passed |
| Migration suite | 6 passed after accommodating historical partial-schema fixtures |
| Album completion, flexible review, migrations and audio specification suites after integration | 37 passed |
| Frontend full suite | 612 passed after updating the completed-album search assertion to include the scope filter |
| Official EBU waveform suite | 23 case groups passed; 24 metric assertions across 22 distinct files |
| Frontend production build | Passed |
| User guide and changelog generation/checks | Passed |
| Local browser flow | Mismatching upload cancellation and one-time submission; delivery cancellation and confirmation; separate producer approval in MasteringView and composer approval in WorkflowStepView; GET verified completed status and both approval timestamps |
| Visual checks | Dark/light themes at 1440 px and 390 px; modal content and buttons fit, no horizontal page overflow |
| R2 analysis worker | Mocked object download, format probing, persisted mismatch and temporary-file cleanup passed; no live R2 bucket upload was performed |

The generated signals cover low-level peaks, peaks in the last incomplete 100 ms, sub-400 ms Integrated validity, short/silent/below-gate audio, per-channel peaks and DC offsets, inter-sample peaks, compressed decoder format, multiple audio streams, and all combinations of 44.1/48/88.2/96/176.4/192 kHz with 16/24/32-bit integer or 32-bit float PCM.

The 997 Hz mono full-scale calibration passes at −3.01 LUFS ±0.1 LU. Generated EBU Tech 3341 table 1 cases 1–5 pass Integrated tolerance ±0.1 LU; generated cases 15–19 pass True Peak tolerance +0.2/−0.4 dBTP. Generated Tech 3342 synthetic cases 1–4 pass LRA tolerance ±1 LU. These are generated signals from published definitions, not the downloaded official waveforms.

An SQLite backup of the actual local database was upgraded to `merge20260919`, joining the audio specification and flexible review migration histories. Comparing every original column preserved all data in 36 existing tables, including 59 tracks, 96 source versions, 40 master deliveries and 521 workflow events; the legacy handoff table contained zero rows. `PRAGMA foreign_key_check` returned no violations. The actual working database was not migrated during this check; normal application startup applies the migration. The migration unit test separately preserves a nonempty legacy handoff table and a `premaster` source record.

## Official EBU waveform results

The user supplied the [official EBU v5 test ZIP](https://tech.ebu.ch/publications/ebu_loudness_test_set), SHA-256 `9cc500b4df83f7c21855c74dce795ef5209a752bf884253ae57d0ce512efb062`. All 23 applicable case groups passed: Tech 3341 Integrated 1–8, True Peak 15–23, and Tech 3342 LRA 1–6, including both authentic programme segments. Integrated case 6 checks both five-channel and six-channel WAVEEX files. The programme files are reused for Integrated and LRA, giving 24 metric assertions across 22 distinct waveforms.

| Metric | Official tolerance | Observed maximum absolute error |
| --- | --- | --- |
| Integrated LUFS | ±0.1 LU | 0.040 LU |
| True Peak | −0.4 / +0.2 dBTP | 0.137 dBTP |
| LRA | ±1 LU | 0.020 LU |

[Machine-readable results](ebu-official-results.json) record every filename, waveform hash, measured value, expected range and analyzer/FFmpeg version. The copyrighted audio files remain outside the repository.

To reproduce with an extracted copy of the official ZIP, run:

```powershell
$env:EBU_LOUDNESS_TEST_DIR = 'E:/test-data/ebu-loudness-test-setv05'
python -m pytest tests/test_ebu_official_audio.py -q --no-cov
```

A configured directory with missing required sequences fails rather than silently skipping. These results validate the listed file-analysis cases on the recorded FFmpeg build; they do not constitute full certification against BS.1770-5/BS.1771/R128. Momentary/short-term display and ballistic tests are outside this UI's scope. Other channel layouts and different FFmpeg builds remain unverified.

## References

- User-provided EBU R128 (2023), ITU-R BS.1770-5 (2023), ITU-R BS.1771-1 (2012).
- [EBU Tech 3341 (2023), table 1](https://tech.ebu.ch/files/live/sites/tech/files/shared/tech/tech3341.pdf).
- [EBU Tech 3342, file-based LRA and minimum tests](https://tech.ebu.ch/files/live/sites/tech/files/shared/tech/tech3342.pdf).
