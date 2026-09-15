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

## Current architecture map

The baseline is a flat-module V1.9 application.  The following are the
responsibilities to preserve while moving them behind V2 boundaries.

| Current module | Responsibility | Current callers / coupling |
| --- | --- | --- |
| gui_app.py | Qt application lifecycle, pages/dialogs/workers, preview fallback, config editing, history/cache UI and dynamic upload dispatch | Imports most shared modules and directly mutates uploader globals |
| path_utils.py | FileSnapshot/FileReadiness, discovery, retry, readiness, natural/mtime sorting, bounded concurrency and cancellable subprocesses | Imported by GUI, state, journal, staging and all uploaders |
| app_config.py | TOML loading, defaults, path resolution, target activation, media/process/scan settings | Imported by GUI, TDLib and all uploaders |
| runtime_paths.py | Resource, executable and writable data roots plus state/cache/log paths | Imported by config, GUI, self-test, uploaders and logging |
| tdlib_common.py | TDLib JSON client, auth/update handling, send result classification, caption/runtime limits and headless UI | Imported by all uploaders and the video CLI-style entry |
| tdlib_video_album_uploader.py | Video discovery/date probes, grouping, thumbnails, preflight, TDLib input and video upload lifecycle | Imported by tdlib_video_app, GUI and tests |
| tdlib_image_album_uploader.py | Image discovery/compression, preflight, photo content, grouping and upload lifecycle | Imported by GUI and tests |
| tdlib_mixed_album_uploader.py | First-level folder grouping, mixed photo/video content and upload lifecycle | Imports image and video uploader internals |
| tdlib_video_app.py | Video-facing orchestration and legacy command-line presentation | Imports video uploader and is invoked by GUI worker |
| album_metadata.py | Caption store, caption composition/validation, filenames and Album keys/plans | Imported by GUI, state and all media uploaders |
| media_identity.py | Snapshot-based media signatures and canonical target identity | Imported by state and journal, with path_utils dependency |
| upload_state.py | Atomic shared checkpoint format for all media kinds | Imports media identity and filesystem helpers |
| upload_journal.py | Atomic per-Album PREPARED/SUBMITTED/CONFIRMED/UNKNOWN records and reconciliation | Imports media identity and filesystem/runtime paths |
| staging.py | Managed network/local staging, marker validation and safe cleanup | Imported by all media uploaders |
| instance_lock.py | One-process lock around upload entrypoints | Imported by GUI and all uploaders |
| app_logging.py / self_test.py | Persistent diagnostics and offline health check | Used by GUI, TDLib and CI/package entrypoints |
| .github/workflows/* | Fast offline/architecture gate and full Windows/macOS packaging gate | Main-Agent-owned CI boundary |

The current high-level flow is:

GUI MainWindow -> ScanWorker/_scan_result -> path_utils or media uploader
scanner -> preview result -> UploadWorker -> media uploader -> tdlib_common
TDJsonClient -> upload_state/upload_journal/staging/app_logging.

The principal V1.9 architecture risk is that each media uploader owns a
slightly different copy of scan, preflight, content construction, journal and
state lifecycle, while gui_app.py reaches into module globals to connect them.

## Target package layout and dependency direction

Phase 0 establishes the destination and ownership; later phases perform the
mechanical migration.

~~~
src/tdlib_media_uploader/
├── app.py                         # main-owned application bootstrap
├── contracts.py                   # main-owned service protocols
├── core/
│   ├── models.py                  # main-owned shared data models
│   ├── sorting.py                 # filesystem Agent
│   ├── filesystem.py              # filesystem Agent
│   ├── readiness.py               # filesystem Agent
│   └── concurrency.py             # filesystem Agent
├── processes/
│   ├── runner.py                  # filesystem/process Agent
│   ├── exiftool.py                # ExifTool Agent
│   └── ffmpeg.py                  # FFmpeg Agent
├── telegram/
│   ├── client.py
│   ├── auth.py
│   ├── target.py
│   ├── limits.py
│   └── send_result.py             # Telegram Agent
├── upload/
│   ├── engine.py                  # main-owned first, then integration
│   ├── planner.py                 # main-owned first, then integration
│   └── preflight.py               # main-owned first, then integration
├── media/
│   ├── image.py                   # ImageStrategy Agent
│   ├── mixed.py                   # MixedStrategy Agent
│   └── video.py                   # VideoStrategy Agent
├── gui/
│   ├── main_window.py             # main-owned application lifecycle
│   ├── models.py
│   ├── events.py
│   ├── workers.py
│   ├── pages/
│   │   ├── video.py
│   │   ├── image.py
│   │   ├── mixed.py
│   │   ├── settings.py
│   │   └── inflight.py
│   └── dialogs/
└── config/
    ├── model.py
    ├── loader.py
    └── paths.py
resources/
└── default_config.toml
~~~

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
