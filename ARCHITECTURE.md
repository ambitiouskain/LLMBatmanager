# LLMBatDesk architecture

LLMBatDesk treats the bytes of each BAT/CMD file as the technical configuration source.
SQLite contains only user metadata, roots, trust hashes, scan cache, and operation history.

Layers:

- `domain`: validated immutable-ish data transferred between layers.
- `parsing`: decoding, continuation folding, token evidence, variable resolution, and backend parsers.
- `discovery`: canonical-path scanning, exclusions, fingerprints, and move candidates.
- `storage`: SQLite metadata/settings/history. No secrets or parsed technical arguments are persisted.
- `runtime`: port inspection, exact-token temporary copies, process execution/tracking, API probes, and logs.
- `services`: orchestration and safety decisions. The GUI calls this layer only.
- `qt`: PySide6/Qt Widgets presentation, Qt models, resize-aware layouts, dialogs, themes,
  high-DPI startup and thread-pool adapters.
- `qt/resources.qrc` and generated `resources_rc.py`: immutable embedded application resources.
  The window icon therefore works in source, OneDir and OneFile without depending on an extraction
  path.
- `resources`: read-only PyInstaller resource lookup only. Persistent writes never use this layer.
- `runnability`: current-machine executable/model validation, kept separate from parsing and runtime.
- `extensions/model_library`: optional, one-way dependency on stable core script records. It owns
  a separate SQLite index, bounded GGUF metadata reader, single cooperative scanner and Qt
  model/view page. Core launch/runtime modules never import extension internals.

Safety boundaries:

1. Parsing never executes a script.
2. Launching accepts a resolved script path, never an arbitrary command string, and uses
   `cmd.exe /d /s /c call "<script>"` with `shell=False`.
3. Stop operations require PID plus creation-time verification and operate only on the recorded tree.
4. Alternate ports are allowed only for a parser-proven literal source; the original bytes are checked
   before and after creating a temporary copy.
5. API checks are low-frequency GET requests and never proxy inference.
6. The model library is lazy and read-only: disabled/unopened states do not import its page, open
   its database, enumerate roots, or create a worker. It never enters the inference path.

## 1.3.x optional model-library boundary

`MainWindow` contains only a string-based lazy import. Enabling the setting adds a plain placeholder;
the first user navigation to that tab imports the extension and opens
`%LOCALAPPDATA%\LLMBatDesk\model_library.sqlite`. A model-library failure is contained in that tab.

The scanner has one cooperative job and no timer or file watcher. Root traversal remains under the
resolved configured root and skips symlinks, Windows junctions, mount points and other reparse
points. The fast cache key is canonical path, byte size and nanosecond mtime; complete model hashes
are deliberately absent.

The GGUF reader accepts common v2/v3 headers and enforces limits for total metadata bytes, key/value
count, tensor count, string length, array items/nesting, tensor dimensions and calculated parameter
overflow. It reads metadata and descriptors sequentially, never seeks into tensor payloads and
closes each file after one record.
Large non-display metadata arrays are validated and streamed/skipped rather than materialized.
Fixed-width arrays use checked byte extents; string arrays validate every bounded element length.

References are derived only from existing `ScriptRecord` parsing output. Explicit, missing, multiple,
dynamic, no-local-GGUF and unparsed states are stored as rebuildable summaries; the extension never
parses or rewrites BAT/CMD independently. User tags/notes stay in the isolated database.
Relationships are role-aware (primary, mmproj, draft, adapter, control) and never bind dynamic or
ambiguous paths to a concrete indexed model.

Model cache identity additionally persists a monotonically increasing reader version, presentation
schema version and outcome class. Only an explicit scan evaluates this cache policy. Old versions
and transient I/O/interruption outcomes are reparsed; current permanent malformed/unsupported
outcomes remain cached until identity/version changes or explicit one-file refresh.

Effective model roles are stored separately from metadata-derived roles. Reference synchronization
can therefore overlay primary/auxiliary roles without rescanning GGUF metadata, while removing a
reference restores the metadata/filename classification. Friendly architecture, nominal size and
quantization presentation use isolated local mappings and cached metadata only.

The existing runtime refresh notifies an initialized page of core state changes. Starting, running,
stopping or detached states cancel/default-block scanning at the next file boundary. This adds no
second process monitor, API poller, priority/affinity change or inference proxy.

## Three independent status axes

`ParseConfidence` describes what can be derived without executing the script. `RunnabilityStatus`
describes whether the statically identified executable and regular model file exist on this computer.
`RuntimeState` describes only current, verified process/socket evidence. A copied script can therefore
be fully parsed, not runnable on this computer, and not running.

## Runtime reconciliation

`ManagedLaunch` rows loaded from SQLite are untrusted candidates. At startup the reconciler requires
matching PID creation time, executable and command line plus a verified server identity. When a port
is known, the listening PID must belong to that verified server/descendant. A surviving `cmd.exe`,
stored PID, configured port, or old `STARTING` value is never sufficient.

Only verified states are returned by `active_launches()`. Pending, failed, stopped, detached and stale
records are shown in launch history. The Qt table models use launch IDs as stable row identities, so
refresh and API callbacks replace model data rather than append duplicate rows.

The Qt GUI uses `QAbstractTableModel` rows keyed by launch ID. Blocked attempts remain operation
history records rather than runtime candidates, so missing resources never enter Active Services.

## Qt presentation boundary

The executable entry calls the Qt high-DPI configuration before constructing `QApplication`.
Windows packaging declares Per-Monitor-V2 DPI awareness. Qt point-sized system fonts, layouts,
`QSplitter`, size policies and style-drawn icons scale at the current screen DPI without bitmap font
scaling.

`QApplication.setWindowIcon()` loads `:/assets/LLMBatDesk.ico` after the resource module is
registered. The same ICO is passed to both PyInstaller specs as the native EXE icon. OneFile may
extract bundled read-only libraries below `sys._MEIPASS`, but `default_data_dir()` only uses an
explicit test override or `%LOCALAPPDATA%\LLMBatDesk`; settings, SQLite, logs and temporary launch
scripts cannot be redirected to the extraction directory implicitly.

Scanning, launching, reconciliation, API checks, port inspection and log reads run through
`QRunnable`/`QThreadPool` adapters. Widgets receive results through queued Qt signals on the GUI
thread. The service layer remains unaware of Qt.

State transitions are defined in `runtime/state_machine.py`. Startup records that cannot be verified
become `STALE_RECORD`; a launcher that exits before a server is identified becomes `FAILED`.

Port inspection is repeated atomically inside `ApplicationService.launch()` before `cmd.exe` is
created. A listening PID is classified as managed only after it matches a verified launch-owned
server identity; otherwise it is an unmanaged conflict.

## 1.2.2 storage, launch and readiness adapters

`runtime.cleanup` owns storage accounting and oldest-first retention. It accepts an explicit set of
active log/temporary paths that can never be deleted. Qt invokes manual and startup cleanup through
`QThreadPool`; terminal runtime transitions invoke the same service method off the GUI thread.

The process executor accepts a validated `LaunchMode`. Background Windows launches combine
`CREATE_NO_WINDOW`, hidden startup information and `CREATE_NEW_PROCESS_GROUP`; visible launches use
`CREATE_NEW_CONSOLE`. Both retain `shell=False` and the existing verified process-tree boundary.

API readiness is stored separately from process state. Each launch has a monotonically increasing
check generation plus checked URL, HTTP status, error, timestamp and retry count. Late results from
older generations are ignored, so a stale Qt callback cannot downgrade an already-ready launch.
The actual runtime port is always used.

`qt.terminal_log` is presentation-only: it preserves original raw text, strips ANSI for display,
classifies severity, linkifies URLs and emits link activation while never altering the stored log.

## 1.2.3 focused presentation adapters

Editor selection stores only a mode and executable path. System mode delegates to the Windows
registered `edit` verb (never the BAT/CMD execution-oriented `open` verb); custom mode resolves an
existing executable and launches the fixed argument vector
`[editor, script]` with `shell=False`. It cannot accept a general command line.

Interactive parsing records command position. An unconditional `pause` after the recognized
long-running server command is marked `harmless_trailing_pause`; pre-launch `pause`, `set /p`,
`choice`, and unresolved branch interaction remain mandatory background-mode warnings.

The Qt launch model uses separate `runtime_display_state()` and `api_display_state()` formatters.
This preserves the backend state machine while preventing API readiness text from leaking into the
process-lifecycle column. Action buttons use the `semanticRole` Qt dynamic property, with dark/light
QSS selectors rather than widget-local styles.

Alternate-port generation is still performed by the service safety boundary. The dialog now starts
generation after availability succeeds, rejects results whose port/generation no longer matches,
and exposes the verified diff as optional presentation.

## 1.3.4 selected-script removal boundary

The Qt action collects an explicit user choice, while `ApplicationService.remove_script()` performs
the authoritative runtime recheck, source classification, fingerprint check, metadata/trust cleanup
and exact-record mutation. Directory-discovered scripts use the core SQLite `ignored_scripts`
table. Every record is validated against its recorded configured root before it can affect discovery;
the scanner receives only exact canonical file paths and skips them before parsing.

`runtime.recycle.QtRecycleBin` is a replaceable file-operation adapter over
`QFile.moveToTrash()`. It has no unlink, shell command or permanent-delete fallback. Launch history
and log files are not part of the removal transaction. The GUI refreshes the existing script model
and model-library reference summaries after success; it does not trigger a GGUF scan.
