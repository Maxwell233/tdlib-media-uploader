# V2.0 Refactor Phase 0 Baseline

## Scope and authority

This document records the main Agent's Phase 0 checkpoint for the V2 GUI-only
refactor.  The pasted V2 execution mode is the controlling boundary:

- The working branch is refactor/v2-gui-only.
- The main Agent owns architecture, shared contracts, integration, tests,
  commits, conflict resolution and final deletion.
- No Subagent was started during Phase 0.
- Subagents may only work in their later assigned ownership sets, must use
  LunaMax when the environment permits a model override, and must stop/report
  when a cross-boundary change is necessary.
- Existing V1.9.1 behavior below is frozen until an explicit migration phase
  reviews and preserves it.

## Repository and baseline evidence

| Item | Result |
| --- | --- |
| Repository | Maxwell233/tdlib-media-uploader |
| Baseline commit | 007968884abb821eed3cfc1e7bd6e64ff7f68e9c |
| Version | 1.9.1 |
| Local branch | refactor/v2-gui-only |
| Remote branch | Created through the connected GitHub repository |
| Host | Darwin arm64 |
| Python | 3.13.15 |
| Locked runtime dependencies | tdjson 1.8.64.post1, Pillow 12.3.0, imageio-ffmpeg 0.6.0, PySide6 6.11.2 |
| Baseline test count | 116 before the Phase 0 contract tests |
| Runtime tests | 116 passed before the Phase 0 contract tests |
| Phase 0 contract tests | 6 added; 122 total passed after the contract and architecture scaffold |
| Source self-test | SELF-TEST OK; optional FFmpeg absent |
| Dependency check | .venv pip check passed |
| Application build | macOS arm64 .app built successfully |
| DMG build | Blocked at hdiutil create: device not configured in this environment |
| Packaged self-test | Blocked by sandbox permission for macOS Application Support data |

The packaged self-test failure is environmental: the frozen app resolved its
data directory to
~/Library/Application Support/TDLib Media Uploader/data and the host denied
directory creation.  The source self-test and the same path rules' unit tests
passed.  The canonical full package gate remains the repository's Windows and
macOS GitHub Actions workflow.

## Frozen regression contract

The refactor must preserve these observable guarantees.  A migration is not
complete when only imports or file locations change; the owning Agent must
compare old and new behavior and run the relevant subsystem plus full offline
tests.

### Data, identity and state

- Source runs use repository/data; frozen Windows uses executable/data; frozen
  macOS uses Application Support/TDLib Media Uploader/data.
- TDLib database and files are data/telegram/database and
  data/telegram/files.  Resource and PyInstaller internal directories are
  read-only and never user-data locations.
- File identity uses relative path, size and mtime_ns from the scan snapshot.
  Album identity also includes media kind, source-root scope and group.
  Identity helpers must not perform a late stat when a snapshot is available.
- UploadState is shared across video, image and mixed modes but remains scoped
  by kind, source root and canonical Telegram target.
- Inflight journals are durable and target-scoped.  PREPARED, SUBMITTED,
  CONFIRMED and UNKNOWN are unresolved until the correct recovery decision;
  an uncertain request must not be automatically resent.
- Checkpoints are written only after a complete Album is confirmed.  State
  write failure keeps the journal for recovery.

### Filesystem and planning

- Natural sorting compares each relative path component, converts consecutive
  digit runs to integers and uses stable deterministic tie-breaking.
- mtime sorting is oldest first and uses the same path comparator for ties.
- Directory discovery skips symlinks and junctions, uses bounded retries,
  captures one stat snapshot per matching file and supports cancellation.
- Readiness distinguishes READY, DEFERRED, UNREADABLE and CHANGED.  Network
  shares use bounded I/O concurrency and short cancellation-aware waits.
- Video scans recurse and group by month or fixed scan order.  Image scans
  recurse and group by configured order.  Mixed scans use only一级子文件夹 as
  independent groups, recursively collect their media, ignore root media and
  never refill an Album across group boundaries.
- An Album plan is fixed before preflight.  Deferred or unreadable files do
  not cause later files to move across the original Album boundary.
- Video date priority is EXIF, optional media creation date, then mtime.
  With read_dates disabled, capture_time remains None, no date probe runs and
  fixed filename grouping remains valid.
- Video and mixed scans enforce the exact 2,097,152,000-byte standard and
  4,194,304,000-byte Premium boundaries.  Images over 10 MiB are skipped
  unless upload-time FFmpeg compression is explicitly enabled.

### Telegram and presentation

- Canonical target identity preserves forum-topic versus channel semantics and
  ignores fields irrelevant to the active target mode.
- Caption length is validated against TDLib's runtime
  message_caption_length_max before a request; user text is not silently
  truncated.
- Partial Album results, immediate failures, cancellation and timeout keep
  their current classification.  UNKNOWN stays visible for manual
  reconciliation.
- Only one upload task runs at a time.  Scan cancellation is explicit and is
  not rendered as a normal empty result.
- GUI filters only the preview; it never changes the upload plan.  Settings
  changes invalidate the scan and require a new scan.
- ExifTool reads only Python-discovered explicit paths, reports progress,
  isolates missing files and preserves valid rows with warnings.  FFmpeg
  metadata, thumbnail and compression calls remain bounded and cancellable.

### Packaging and operations

- The GUI is the only product entrypoint after Phase 1.  Packaged
  --self-test remains available and offline.
- VERSION is the single version source.
- Windows x64 and macOS arm64 builds include required notices, a verified
  LGPL/nonfree-free FFmpeg binary, and pass package validation in CI.
- The repository's workflow remains the full gate; local environment-specific
  hdiutil or Application Support failures must not be papered over by changing
  product behavior.
- V2 refactor pull requests and pushes to refactor/v2-gui-only use the
  lightweight offline/architecture gate.  Windows and macOS packaging remains
  the full gate on the existing platform workflow.

## Compatibility architecture map

The original V1.9 behavior was implemented as flat top-level modules. That
behavior now lives under the package boundary below `src/`; this map records
the compatibility owners that remain intentionally visible to the V2
adapters.

| Current module | Responsibility | Current callers / coupling |
| --- | --- | --- |
| `src/tdlib_media_uploader/gui/main_window.py` | Qt application lifecycle, compatibility pages/dialogs/workers, preview fallback, configuration editing, history/cache UI and upload dispatch | Composition host for the GUI; delegates scans/uploads to package workers and integration services |
| `src/tdlib_media_uploader/core/filesystem_legacy.py` | FileSnapshot/FileReadiness, discovery, retry, readiness, natural/mtime sorting, bounded concurrency and cancellable subprocesses | Compatibility implementation used by the legacy media builders and GUI helpers |
| `src/tdlib_media_uploader/config/loader.py` | TOML loading, defaults, target activation and media/process/scan settings | Imported by GUI, TDLib compatibility code and media builders |
| `src/tdlib_media_uploader/config/paths.py` | Resource, executable and writable data roots plus state/cache/log paths | Imported by config, GUI, self-test, upload adapters and logging |
| `src/tdlib_media_uploader/telegram/tdlib_common.py` | TDLib JSON client, auth/update handling, send-result classification, caption/runtime limits, diagnostics and headless UI | Compatibility transport used by the media builders and GUI upload bridge |
| `src/tdlib_media_uploader/media/legacy_video.py` | Video discovery/date probes, grouping, thumbnails, preflight and TDLib input construction | Called by `media/video.py` and the GUI integration bridge |
| `src/tdlib_media_uploader/media/legacy_image.py` | Image discovery/compression, preflight, photo content and grouping | Called by `media/image.py` and the GUI integration bridge |
| `src/tdlib_media_uploader/media/legacy_mixed.py` | First-level folder grouping, mixed photo/video content and input construction | Called by `media/mixed.py`; shares the video/image compatibility builders |
| `src/tdlib_media_uploader/core/album.py` | Caption store, caption composition/validation, filenames and Album keys/plans | Imported by GUI, state, journal and media compatibility code |
| `src/tdlib_media_uploader/core/identity.py` | Snapshot-based media signatures and canonical target identity | Imported by state and journal |
| `src/tdlib_media_uploader/core/upload_state.py` | Atomic shared checkpoint format for all media kinds | Imports identity and filesystem helpers |
| `src/tdlib_media_uploader/core/upload_journal.py` | Atomic per-Album PREPARED/SUBMITTED/CONFIRMED/UNKNOWN records and reconciliation | Imports identity and package path services |
| `src/tdlib_media_uploader/upload/staging.py` | Managed network/local staging, marker validation and safe cleanup | Imported by media compatibility builders and upload integration |
| `src/tdlib_media_uploader/core/instance_lock.py` | One-process lock around the packaged upload entrypoint | Imported by the GUI composition host and upload compatibility code |
| `src/tdlib_media_uploader/core/logging.py` / `core/self_test.py` | Persistent diagnostics and offline health check | Used by GUI, TDLib compatibility code and CI/package entrypoints |
| .github/workflows/* | Fast offline/architecture gate and full Windows/macOS packaging gate | Main-Agent-owned CI boundary |

The V2-integrated high-level flow is:

GUI MainWindow -> ScanWorker -> gui.integration.scan_v2 -> media strategy
-> preview result -> UploadWorker -> gui.integration.run_v2_upload
-> UploadEngine -> TDLibSender/tdlib_common -> upload_state/upload_journal/
upload/staging and core/logging.

The package GUI still renders the established dictionary-shaped preview
contract, but the scan and upload workers cross one explicit V2 GUI boundary.
The compatibility modules remain behind the strategy and sender adapters for
media-specific probing and TDLib input construction; they no longer own the
GUI upload lifecycle. The old root-level Python launchers and wrappers are no
longer part of the repository.

## Target package layout and dependency direction

Phase 0 establishes the destination and ownership; later phases perform the
mechanical migration.

~~~
src/tdlib_media_uploader/
├── __init__.py                    # public package exports
├── app.py                         # packaged application bootstrap
├── contracts.py                   # shared service protocols
├── config/
│   ├── __init__.py
│   ├── model.py
│   ├── loader.py
│   └── paths.py
├── core/
│   ├── __init__.py
│   ├── models.py                  # shared data models
│   ├── sorting.py                 # deterministic path ordering
│   ├── filesystem.py              # V2 filesystem services
│   ├── filesystem_legacy.py       # compatibility filesystem services
│   ├── readiness.py
│   ├── concurrency.py
│   ├── album.py
│   ├── identity.py
│   ├── upload_state.py
│   ├── upload_journal.py
│   ├── instance_lock.py
│   ├── logging.py
│   ├── self_test.py
├── processes/
│   ├── __init__.py
│   └── runner.py                  # bounded external-process execution
├── telegram/
│   ├── __init__.py
│   ├── client.py
│   ├── auth.py
│   ├── target.py
│   ├── limits.py
│   ├── send_result.py
│   └── tdlib_common.py             # compatibility TDLib transport
├── upload/
│   ├── __init__.py
│   ├── engine.py                  # main-owned first, then integration
│   ├── planner.py                 # main-owned first, then integration
│   ├── preflight.py                # main-owned first, then integration
│   └── staging.py                  # managed local/network staging
├── media/
│   ├── __init__.py
│   ├── image.py                   # ImageStrategy adapter
│   ├── mixed.py                   # MixedStrategy adapter
│   ├── video.py                   # VideoStrategy adapter
│   └── legacy_*.py                # media-specific compatibility owners
├── gui/
│   ├── __init__.py
│   ├── application.py             # main-owned lazy GUI bootstrap
│   ├── events.py                   # Qt signal and auth bridges
│   ├── integration.py             # Qt-free V2 workflow bridge
│   ├── models.py                  # V2 preview translation
│   ├── workers.py                  # scan/upload thread lifecycle
│   ├── main_window.py             # application composition and lifecycle
│   ├── pages/
│   │   ├── __init__.py
│   │   ├── home.py
│   │   ├── task.py
│   │   ├── upload.py
│   │   ├── video.py
│   │   ├── image.py
│   │   ├── mixed.py
│   └── (settings, history, inflight and dialogs remain composed by main_window.py)
resources/
└── default_config.toml
~~~

The current GUI keeps those compatibility surfaces together in
`gui/main_window.py` because they share the established page and dialog
contracts. They are still GUI-only code; no lower-level service imports them.

Allowed dependency direction:

~~~
app
├── gui
├── upload
│   ├── media
│   ├── telegram
│   ├── processes
│   └── core
├── config
└── core

core -> standard library only
config -> core (types only where needed)
processes -> core
telegram -> core and processes only where transport diagnostics require it
media -> core, config, processes
upload -> core, config, telegram, processes, media contracts
gui -> app, upload contracts, config, core event models
~~~

No lower layer may import gui.  Media strategies may not own journal/state
lifecycle.  Telegram transport may not decide filesystem grouping.  Shared
models and event types are imported from the main-owned contract package; no
subagent may redefine them.

## Public V2 contracts

The import-stable names are implemented in
src/tdlib_media_uploader/core/models.py and contracts.py:

- Data models: FileSnapshot, MediaItem, AlbumPlan, UploadBatchResult,
  ProgressEvent, LogEvent, AuthEvent, ScanResult and UploadRunResult.
- Service protocols: UploadEngine, MediaStrategy, EventSink and CancelToken;
  UploadContext carries the injected run collaborators.
- Durable send states: PREPARED, SUBMITTED, CONFIRMED, FAILED and UNKNOWN via
  BatchStatus.

Important semantics:

1. FileSnapshot is captured by discovery and is the only identity input for
   Album/state/journal hashing when available.
2. MediaItem carries media kind, source-root scope and the captured snapshot;
   capture_time is optional and must remain None when date reading is disabled.
3. AlbumPlan contains the complete immutable boundary and the pending subset.
   Preflight may mark items deferred but may not refill the plan from later
   files.
4. UploadEngine owns the run lifecycle and durable side effects.  A strategy
   only scans, plans and translates media-specific Telegram content.
5. EventSink and CancelToken keep workers/UI adapters out of core and media
   algorithms.

## Phase 5 engine checkpoint

The main Agent owns the first reference implementation in
`src/tdlib_media_uploader/upload/engine.py`, `planner.py` and `preflight.py`.
The engine keeps the full `AlbumPlan.items` boundary immutable, narrows only
`pending_items`, and invokes state/journal/sender/staging collaborators through
`UploadContext`.  A confirmed send is checkpointed in this order:

~~~
PREPARED -> SUBMITTED -> CONFIRMED -> state checkpoint -> journal finalize
~~~

The reference stores are in-memory test adapters.  The existing V1.9 durable
state, journal and TDLib implementations remain unchanged until the Image,
Mixed and Video strategy migration wave supplies explicit adapters.

## Phase 6 GUI integration checkpoint

`src/tdlib_media_uploader/gui/integration.py` now supplies the GUI boundary
for all three strategies.  `scan_v2()` returns immutable V2 scan/planning
models, while `run_v2_upload()` injects the durable legacy state and inflight
journal into `UploadEngine`, adapts TDLib sends without enabling the legacy
client journal, and translates progress/events back to the existing task
center. A confirmed engine checkpoint is the only path that removes staged
media. `gui/main_window.py` keeps the dependency-free preview fallback for
environments where the V2 strategy dependencies cannot be imported.

## Phase 7 GUI bootstrap checkpoint

`src/tdlib_media_uploader/gui/application.py` now owns the package-facing
`main()` and `run_self_test()` entrypoints. The self-test path loads only the
offline checker, so it does not require Qt; a normal launch lazily delegates
to `gui/main_window.py`. `src/tdlib_media_uploader/app.py` and the PyInstaller
spec both point at this package boundary, while the existing GUI pages remain
behaviorally compatible under `gui/pages/`.

## Phase 8 GUI worker checkpoint

`src/tdlib_media_uploader/gui/events.py` now owns the authentication and task
center signal bridges, and `gui/workers.py` owns the scan/upload `QThread`
lifecycles. The package GUI supplies explicit scan, target, source-root and
configuration callbacks; the workers never import the window composition
module. This keeps cancellation and result mapping in one place without
changing the existing page signals or preview dictionary contract.

## Phase 9 GUI preview-model checkpoint

`src/tdlib_media_uploader/gui/models.py` now owns the V2 preview translation:
item identity, immutable plan projection, group aggregation, byte totals and
scan warning projection. `gui/main_window.py` retains the compatibility
helpers needed by the existing dictionary-shaped page contract, so the model
boundary remains Qt-free.

## Phase 10 GUI page checkpoint

The package owns the overview and task-center widgets in `gui/pages/home.py`
and `gui/pages/task.py`. Both pages keep their existing signals and public
update methods, while their formatting and version lookup are local to the
package boundary. `gui/main_window.py` composes these classes and preserves
the remaining settings, inflight, history and dialog compatibility surfaces.

## Phase 11 GUI upload-page checkpoint

The shared media upload widget now lives in `gui/pages/upload.py`, with
explicit `VideoPage`, `ImagePage` and `MixedPage` route classes.  The page
consumes the existing preview dictionary and signal contracts, while its
configuration, caption persistence and dialog hooks are supplied through
`UploadPageServices`. `gui/main_window.py` keeps the old `UploadPage(kind)`
constructor as a compatibility shell and injects the legacy patch points;
`MainWindow` instantiates the package-owned route classes.

## Ownership table for the first parallel wave

| Agent | Allowed files after Phase 0 confirmation | Forbidden areas | Depends on |
| --- | --- | --- | --- |
| LunaMax A | core/sorting.py, core/filesystem.py, core/readiness.py, core/concurrency.py, processes/runner.py and their migrated tests | gui, telegram, upload, media strategies, shared models | public contracts above |
| LunaMax B | telegram/client.py, auth.py, target.py, limits.py, send_result.py and their migrated tests | gui, filesystem implementation, UploadEngine, media strategies | public contracts above |
| LunaMax C | moved/split tests for filesystem, sorting, readiness, process runner and telegram; no business implementation | every implementation module | source baseline and contracts |

The package scaffold, shared contracts, tests/test_architecture_contract.py and
all workflow files remain main-Agent-owned.  No parallel Agent may modify them.

This is the Phase 3 plus Phase 4 first wave.  It is the only parallel wave
authorized immediately after user confirmation.  Integration order is:

1. Review each worktree diff and reject ownership violations.
2. Run filesystem/process subsystem tests and Telegram subsystem tests
   separately.
3. Run the complete offline suite and import checks.
4. Resolve duplicate implementations and update the target branch.
5. Commit one coherent phase checkpoint on refactor/v2-gui-only.

The main Agent must not start this wave until the user confirms the Phase 0
checkpoint, as required by the execution mode.
