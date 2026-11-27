# GUI and runner audit

This audit covers the browser client, local HTTP server, GUI process manager, macOS request queue and receive supervisor. Validation uses isolated queues, fake receiver subprocesses and fault injection. It does not establish RF performance or driver/hardware reliability.

## Corrected findings

| Area | Finding | Correction |
| --- | --- | --- |
| GUI startup | An unrelated HTTP 200 on the configured port could be mistaken for successful startup. | Readiness must match the newly launched process ID and random instance ID. A failed launch reaps its own child and leaves unrelated servers alone. |
| GUI stop | A bare PID and loose command substring could target a reused PID or lose ownership metadata. | PID, process start/command stamp, instance ID and checkout/session identity are verified. Ambiguous ownership is refused. Metadata is retained on failed termination. |
| Service concurrency | Concurrent start/stop operations could launch duplicate servers or overwrite the PID file. | Kernel file lock around lifecycle operations and atomic metadata publication. |
| Service diagnostics | Append-only diagnostics could grow without a bound. | Runtime diagnostics rotate at 2 MiB with two backups. Startup output rotates separately at 512 KiB. |
| Runner exclusivity | Directory/PID locks had stale-file and startup races. | A kernel lock serializes runner instances, independently of leftover lock files. |
| Runner death | Killing the runner could leave an RX child while a new runner began another job. | The job shell inherits the runner lease. The receive supervisor passes a separate receiver lease to its RX child; a killed supervisor does not release that child's lease. New hardware jobs wait in the queue. |
| Runner interruption | Running status could remain forever, or an accepted job could be replayed after a crash. | Interrupted status is published on orderly termination. Stale running statuses are reconciled only after acquiring the runner lock. Claimed requests are retained and never replayed automatically. |
| Request parsing | Partial validation allowed out-of-range values, unknown keys or conflicting operations. | Native property-list parsing validates the complete schema before dispatch. Shell entry points also reject conflicting workflows and invalid Boolean flags. |
| Status publication | Direct writes could expose partial JSON; path/quote characters could produce invalid status files. | JSON serialization, fsync and atomic rename. |
| Queue publication | Predictable temporary names could collide. | Unique temporary request files and atomic publication. |
| Launch agent wakeup | A missed filesystem wakeup could strand a pending request. | RunAtLoad plus a 15-second periodic backstop alongside WatchPaths. Installer refuses to replace an active runner. |
| Runner selection | A loaded launch-agent label could be accepted even when it watched a different checkout or data directory. | GUI readiness checks the agent's configured repository and sessions paths. |
| GUI admission | Two GUI processes could both pass an in-memory busy check. | Shared admission lock and a fresh queue/runner/receiver-lease check. |
| Control mailbox | Separate GUI instances could overwrite pending pause/resume requests. | Per-session kernel lock and acknowledgement checks. Abort retains priority. |
| Retry semantics | An active-attempt retry could become a full new scan if the session ended while its confirmation was open. | Confirmation captures attempt/repeat mode; the server rejects a changed state. |
| Duplicate requests | A repeated HTTP mutation could issue duplicate work after a lost response. | Client request IDs and a bounded five-minute, per-server response cache. Reused IDs with changed parameters are rejected. Mutations are not automatically retried after uncertain timeouts. |
| Dead supervisors | Stale manifests could leave live controls enabled. | Process/session identity is checked. The view marks interrupted acquisition and disables controls without rewriting the original evidence. |
| HTTP resource use | Unlimited request threads, incomplete POSTs and whole-file report reads could exhaust resources. | Twelve workers, eight-second socket timeouts, bounded JSON bodies, overload rejection, 64 KiB report streaming, HEAD and single byte-range support. |
| Display buffers | Parseable but unterminated JSONL fragments could appear as completed frames. | Only newline-terminated records enter the preview. The raw tail read is capped at 2 MiB. |
| Spectrum fidelity | Masked numeric bins could be plotted; pooled frequency positions were approximate. | Explicit masks remain gaps, missing values are not interpolated, and pooled display points carry their actual frequency centers. |
| Receiver thresholds | Fixed reference thresholds could be mistaken for the active saved guard policy. | Meters use the saved per-record guard limits; absent limits remain unknown. Missing LO/rate metadata is not converted to zero. |
| Data qualification | An in-progress raw frame could be labelled as accepted without checking its quality evidence. | Preview qualification checks complete measurement status, continuity, headroom and RF validity. Unknown evidence remains unknown. |
| Memory retention | Session history and cached summaries grew with every viewed session. | Twelve history contexts, sixty sampled visits per context and thirty-two small summary entries. Only required summary fields are cached. |
| Large failure evidence | Classification loaded complete logs and IQ evidence files into RAM. | Failure classification and SHA-256 validation stream bounded chunks. |
| Browser scheduling | Overlapping poll loops, missing deadlines and stale results complicated connection recovery. | One scheduled poll sequence, request cancellation, deadlines, bounded backoff, foreground refresh and selection epochs. |
| Browser controls | Disconnected/stale pages could retain actionable controls; repeat-after-abort and selection races were confusing. | Controls require fresh matching snapshots, local in-flight state and supervisor capabilities. Repeat-after-abort is supported; newly created sessions are selected automatically. |
| Browser rendering | Every poll rebuilt focused table rows and redrew unchanged charts. | Stable table snapshots preserve keyboard focus. Chart redraws occur only when data or layout changes. |
| Presentation | Large promotional headings, decorative glyphs, gradients and compressed source obscured the operating state. | Neutral application chrome, compact acquisition totals, dark measurement plots, explicit state text and readable static HTML/CSS/JavaScript. |

## Runtime bounds

- GUI HTTP concurrency: **12** requests; additional connections receive HTTP 503.
- Socket inactivity timeout: **8 seconds**. The browser uses **7 seconds** for reads and **15 seconds** for submitted actions.
- Request body: **16 KiB**. Metadata JSON read: **12 MiB**. Spectrum tail window: **2 MiB**. Acquisition log preview: **16,000 bytes**.
- Spectral preview: at most **600** display groups per visit. IF history: at most **60 visits × 12 sessions**. These are sampled display representations, not the original measurement archive.
- Report streaming chunk: **64 KiB**. Failure-log scanning: **64 KiB** chunks with overlap. Evidence hashing: **1 MiB** chunks.
- Mutation cache: **128** successful request IDs, expiring after **five minutes**. This is not a persistent transaction log across server restarts.
- GUI startup wait: **10 seconds**. Verified GUI stop uses an **eight-second** grace period before a bounded kill fallback.
- Periodic queue wakeup: **15 seconds**, in addition to filesystem notifications.

## Operating and recovery

Install/update the queue launch agent while no job is active:

```sh
bash tools/macos_runner/install.sh --no-probe
```

The installed shim now delegates to `tools/macos_runner/runner.py` in the checkout. An installation created before this change needs this one-time shim/launch-agent update; pulling source files alone does not replace an already copied legacy shim.

The GUI service can be managed independently of the RF queue:

```sh
python -m soapypower.gui_service start --repo /absolute/checkout --sessions /absolute/sessions --port 8765
python -m soapypower.gui_service status --repo /absolute/checkout --sessions /absolute/sessions
python -m soapypower.gui_service stop --repo /absolute/checkout --sessions /absolute/sessions
```

Queued `--gui start|stop` requests remain supported, but wait behind an active queue job. Direct service commands do not stop an independent RF acquisition. A legacy GUI with only `gui.pid` is reported as unmanaged; it is not automatically adopted or killed.

A held receiver lease after a supervisor crash is intentional. Inspect the retained session and owning receive process before terminating that process explicitly. Deleting the lock file does not release a kernel lock and can undermine coordination; do not delete lock files to force another scan.

## Limits

- Local filesystems, the OS, USB transport and native SDR drivers can still stall or fail. A GUI cannot prove that hardware is electrically idle after a native failure. Blocking filesystem operations are not forcibly cancelled inside HTTP worker threads.
- Only cooperating runner/supervisor paths participate in the receiver lease. Independently launched SDR tools can still contend for hardware.
- The GUI's latest-frame view does not recreate a coherent RF/phase history. Pauses, retunes, manual retries and missing intervals remain separate measurement contexts.
- Stop/start ownership verification deliberately refuses ambiguous or reused PIDs. Some legacy process cleanup requires operator inspection.
- GUI process health is checked at startup and by the explicit status command. This service manager is not a continuous crash-restart watchdog. The scan queue has its own launchd supervision and restart backstop.
- A timeout after a submitted mutation has an uncertain outcome. Inspect the job/status before issuing another request. Request-ID replay protection is per server process, not durable exactly-once delivery across a crash.
- Software fault injection does not substitute for a live hardware qualification run. No RF scan is required by this audit workflow.
