# PHI Console backend threat model

`docs/THREAT_MODEL.md` scopes itself explicitly to `phi_engine`, the
standalone pipeline package (see its own "System boundary" section: "In
scope, all under `phi_engine/`"). This document covers the second half of
the codebase `docs/THREAT_MODEL.md` does not: the `backend`/`frontend` PHI
Console service, specifically the raw-data sandbox boundary
(`phi_core/control/sandbox.py`), the process-network isolation it relies on,
the filesystem trust boundary that boundary sits inside, and the crypto/cookie
custody in `phi_core/crypto.py`. It is a security and trust artifact, not a
regulatory certification, matching the framing `docs/THREAT_MODEL.md` already
uses.

Written during Wave R-b of the Phase R remediation. `docs/PHASE_STATUS.md`'s
defect table (`D1`-`D9`) is the authoritative, live record of exactly which
sandbox defects are fixed as of any given commit. `D1`, `D2`, `D3`, `D7`, and
`D9` were remediated by a concurrent subagent (R-Sandbox) during this same
wave and are verified fixed as of this document's own final revision
(checked directly against the live `sandbox.py`); `D5` and `D6` remain open
by design (see section 5 below and `docs/PHASE_STATUS.md`). Check
`docs/PHASE_STATUS.md` for current status before treating any item below as
still open regardless.

## 1. The macOS memory-limit gap and its explicit override switch

`SandboxManager` (`sandbox.py`) is supposed to bound a raw-data worker's
memory with `resource.setrlimit(RLIMIT_AS, ...)`, alongside the CPU-time
(`RLIMIT_CPU`) and wall-clock ceilings D1/D2 concern themselves with. On
Darwin/XNU, `RLIMIT_AS` cannot reliably be lowered to the configured sandbox
ceiling: the kernel rejects a value below the process's already-mapped
virtual address space with `EINVAL` (CPython mistranslates this as
`ValueError`), and a modern Python process's own shared-library mappings can
already exceed a 1 GiB default ceiling before the sandbox worker does
anything. This is a documented CPython/XNU interaction (CPython issue
78783), not a bug specific to this codebase.

The mitigation is fail-closed by default and explicit when overridden: a
platform capability probe runs once at import time
(`_probe_memory_limit_enforceable`), and sandbox creation refuses outright
(raises rather than silently degrading) when the ceiling cannot be enforced,
unless an operator explicitly sets `PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1` to
accept the gap (the documented case: local development on macOS). When the
override is used, every resulting `SandboxRecord` carries
`memory_limit_enforced=False`, so the gap is durably recorded per run rather
than silently assumed away -- a run that accepted unenforced memory is
distinguishable, after the fact, from one that had a real ceiling.

**Operational implication:** in any deployment where
`PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY=1` is set (which, on this platform,
production included, may be the only way to run at all), a malicious or
buggy raw-data-processing payload can exhaust host memory. CPU time
(`RLIMIT_CPU`) and wall-clock (`proc.join(max_wall_seconds)` + `proc.kill()`)
remain enforced regardless of this switch, bounding the *duration* of such an
event even when its *memory footprint* is unbounded. A production deployment
that needs a real memory ceiling needs a platform where `RLIMIT_AS` is
actually enforceable (Linux), or an external control (a cgroup, a container
memory limit, a separate OS-level supervisor) outside what this module alone
can provide on Darwin.

## 2. The `socket.socket` monkeypatch: what it stops and what it does not

`_deny_sockets()` replaces the module attribute `socket.socket` with a
function that always raises, inside the freshly-spawned (`multiprocessing`
`spawn` context, never `fork`) sandbox worker process, before the worker's
own `func` runs. This is a real, verified control, and it is also narrower
than "network access denied" reads at a glance. Verified directly against
this codebase's own venv (Python 3.11.16):

**Genuinely blocked** (all resolve the module-global name `socket.socket` at
call time, after the patch is in place, so the patched function is what
runs):

- `urllib.request.urlopen` (raises `URLError` wrapping the patch's `OSError`)
- `socket.create_connection`
- `socket.socketpair` / socket construction from a detached raw fd -- the
  stdlib's own `socketpair()` wrapper calls the module-global `socket(...)`
  constructor internally to wrap the raw pair it gets from `_socket`, so the
  patch still intercepts it
- any library (`httpx`, `requests`, etc.) that calls through
  `socket.socket(...)` or the above rather than around them

**Not blocked** (each independently verified against this repository's
Python 3.11.16 venv):

- **`import _socket; _socket.socket(...)`** -- the C accelerator module
  underneath `socket.py`. `socket.socket = _raise` patches only the
  `socket` module's own attribute; `_socket` is a separate module object
  the patch never touches, and it exposes the same raw socket-creation
  primitive directly.
- **`importlib.reload(socket)`** -- re-executes `socket.py`'s module body
  in place, which re-binds `socket.socket = _socket.socket` (the real
  class), silently undoing the patch for the remainder of the process.
- **`subprocess.Popen`, `os.system`, `os.execve`** -- these start or become
  a *new* process image that never imported the patched `socket` module in
  the first place (or, for `execve`, replaces the current image entirely).
  The sandbox worker does not set `RLIMIT_NPROC`
  (`_child_entry` sets only `RLIMIT_CPU`, `RLIMIT_FSIZE`, and conditionally
  `RLIMIT_AS`; there is no process-count ceiling), so this is not merely
  "unpatched," it is also unbounded in how many such processes a malicious
  payload can start.
- **`ctypes`** calling the platform's `socket(2)` syscall directly through
  `libc` -- bypasses Python's `socket` module entirely, no patch to evade.
- **A pre-patch reference captured while unpickling the `multiprocessing`
  `spawn` bootstrap's own arguments** -- `run_isolated` pickles `func`,
  `args`, and `kwargs` in the parent and unpickles them in the child as
  part of `multiprocessing`'s own bootstrap, before `_child_entry`'s body
  (and therefore `_deny_sockets()`) ever executes. An object whose
  `__reduce__`/`__setstate__` has a network side effect during that
  unpickling step runs before the patch is in place. This is a narrow,
  structural window inherent to how `multiprocessing.spawn` bootstraps a
  target, not something `_deny_sockets()`'s placement inside `_child_entry`
  can close by being called earlier.

**`os.fork()` is explicitly not a bypass.** A worker calling `os.fork()`
creates a child that is a copy of the *already-patched* parent process
image, `socket.socket` included -- fork duplicates process memory, it does
not re-import modules. Anyone reasoning about this control from "fork
escapes monkeypatches" general folklore would draw the wrong conclusion
here; the actual bypasses are the five listed above, not `fork()`.

**Bottom line:** `_deny_sockets()` stops *accidental* egress by cooperative
Python code calling the ordinary, documented socket APIs. It stops nothing
a deliberately malicious payload chooses not to route through those APIs.
Treat it as a defense against a buggy or careless raw-data-processing
routine, not as a security boundary against an adversarial one.

## 3. Same-uid filesystem reality

The sandbox worker process runs as the *same OS user* as the parent backend
process (no `setuid`, no separate service account, no container/namespace
boundary -- `multiprocessing`'s `spawn` context starts a new process, not a
new user). Unix filesystem permission bits (`0700`, `0600`, etc.) are only
meaningful *between different users or groups*; they grant or restrict
nothing to a process running as the same uid that already owns the file. A
sandboxed raw-data worker can therefore read:

- `backend/.env` (contains `MONGO_URL`, `APP_ENCRYPTION_KEY`,
  `ATTESTATION_SIGNING_KEY`, and any configured LLM provider API keys)
- `~/.aws/credentials` and any other AWS credential file
- `~/.ssh/` (private keys, `known_hosts`, agent socket path)
- any mounted service-account token file (e.g. a Kubernetes projected
  service-account token, a GCP/Azure workload-identity token file) readable
  by that uid

`validate_sandbox_path` (`sandbox.py`) constrains *callers* -- it refuses to
let the rest of the codebase hand the worker a path outside its own
workspace -- it does not, and cannot, constrain what the worker's own code
reads once it is running as that uid. The env-var allowlist (`_ALLOWLISTED_ENV_KEYS`,
D7 -- see section 7 for how this superseded the earlier denylist design) reduces what
the child's *environment variables* carry; it has no
bearing on what the child can open directly from the filesystem by path,
which is the entire class of exposure this section describes. A real
process-identity or container/namespace boundary (a dedicated low-privilege
uid, a Linux user namespace, a container with its own filesystem view) is
the only way to close this gap; nothing in the current `SandboxManager`
attempts to.

## 4. D9: sandbox directory tree permissions, intermediate directory caveat

`create_sandbox()` originally built each run's workspace as `SANDBOX_DIR /
run_id / uuid4().hex` via a single `workspace.mkdir(parents=True,
exist_ok=False, mode=0o700)` call, followed by an explicit
`os.chmod(workspace, 0o700)` on the leaf only. `Path.mkdir(parents=True,
mode=...)` applies the given `mode` to the *final* directory it creates
only; any intermediate directory it creates along the way (here,
`SANDBOX_DIR / run_id`, on that run's first sandbox) was created with the
platform default mode (`0o777` as modified by the process umask, commonly
`0o755`), not `0o700`. R-Sandbox fixed this in this same wave: `create_sandbox`
now creates the `run_id` intermediate directory with an explicit
`os.chmod(run_dir, 0o700)` of its own, so every level of the tree is `0700`,
not just the leaf. Verified directly against the current `sandbox.py`.

**Why the gap was contained rather than actively exploitable while it was
open:** `phi_core.paths.SANDBOX_DIR` itself is created with an explicit
`mkdir(..., mode=0o700)` *and* a following `os.chmod(SANDBOX_DIR, 0o700)` at
module-import time (`phi_core/paths.py`), so a different OS user could never
traverse into `SANDBOX_DIR` at all regardless of what mode any `run_id`
subdirectory ended up with -- Unix requires execute permission on every path
segment to reach one further in, and `SANDBOX_DIR` itself denied that to
anyone but its owner. That containment held only because of `SANDBOX_DIR`'s
own mode, not because the `run_id` level was itself correctly restrictive; a
future refactor that changes where `SANDBOX_DIR` sits in the filesystem, or
who else might share its parent, should not assume every level below it is
`0700` by construction without checking, even now that this specific
instance is fixed -- the same class of gap (an intermediate `mkdir` call
relying on `parents=True` without an explicit `mode` per level) could
recur anywhere else in the codebase that builds a nested path the same way.

## 5. D6: cookie expiry and HMAC domain-separation gap

`crypto.py`'s `pseudonym_salt(session_id)` and the signature half of
`sign_principal_cookie(principal)` are both computed as `hmac.new(key,
<input>.encode(), hashlib.sha256).hexdigest()` under the exact same
server-held key, with no domain separator (no fixed context string, prefix,
or purpose tag mixed into either HMAC input). For the same input string, the
two outputs are byte-identical: `pseudonym_salt("alice")` and the signature
component of `sign_principal_cookie("alice")` are the same hex string,
verified directly against the running code. An attacker who can obtain one
value under a given input string (for example, a `pseudonym_salt` computed
for a `session_id` an attacker also controls, or can guess) obtains a valid
forged principal-cookie signature for a principal string equal to that same
input, without the server ever having signed a cookie for that principal --
the two "different" HMAC uses are, cryptographically, the same function
applied to the same key and the same message space, with nothing to keep
them apart.

Separately, `sign_principal_cookie` produces `<principal>.<hmac-hex>` with no
issued-at timestamp, no nonce, and no key version embedded in the cookie
value itself. A cookie signed under a given `APP_ENCRYPTION_KEY` remains
valid forever (bounded only by however long the browser/client chooses to
retain it) with no server-side mechanism to expire it, detect replay, or
distinguish which key version signed it once key rotation exists. This is
recorded here as open, disclosed residual risk carried until Phase 8, per
this rewrite's phase plan; it is not remediated by anything in Wave R-b.

## 6. `crypto.py` dev-key auto-generation orphans existing ciphertext

`_load_or_create_key()` (`crypto.py`) is the single key-loading path behind
every encryption/signing operation in this module (`encrypt_api_key`,
`decrypt_api_key`, `encrypt_reversal_map`, `decrypt_reversal_map`,
`pseudonym_salt`, `sign_principal_cookie`/`verify_principal_cookie`,
`egress_digest_key`). When `APP_ENCRYPTION_KEY` is unset and `PHI_ENV=dev`,
it silently generates a fresh random Fernet key, attempts to append it to
`backend/.env` so a later process picks up the same key, and sets it in the
current process's environment either way.

The failure mode: whenever that generated key does not persist and get
reused by the *next* process -- because the append to `backend/.env` fails
(read-only filesystem, ephemeral/ containerized dev environment with no
writable `.env`), because `.env` itself gets reset or regenerated, or
because `APP_ENCRYPTION_KEY` is unset again by any later process start --
the next process generates a *different* new key. Every ciphertext produced
under the previous key (stored API keys, stored reversal maps/pseudonym
mappings, any cookie signed under the old key) becomes permanently
undecryptable under the new one: `decrypt_api_key`/`decrypt_reversal_map`
raise `KeyRotated`, and `verify_principal_cookie` silently rejects every
cookie signed under the old key as if it had never been issued. Nothing
logs or surfaces this as an event; it presents only as scattered
`KeyRotated` exceptions or "why did every session log itself out" reports,
with no direct link back to a key having silently regenerated. This is a
`PHI_ENV=dev` convenience path only -- production refuses to boot at all
without an explicit `APP_ENCRYPTION_KEY` (`_load_or_create_key`'s own
`RuntimeError` branch) -- but any development or CI environment that runs
with an ephemeral filesystem hits it routinely, silently, and without a
diagnostic trail connecting the symptom to the cause. A corresponding
one-line pointer is added to `docs/RUNBOOK.md` so an operator hitting this
symptom has somewhere to look.

## 7. Phase 9 confirmation: worker-credential criterion and its residual risk

Phase 9 (`agents/reasoning.py::Executor`, `control/execution_validators.py`)
re-checked docs #50-52's worker-credential criterion -- "the worker process
receives no credential in its environment or its arguments" -- against the
live `sandbox.py` rather than assuming section 3's description above still
matches the code. It does, with one correction: section 3 above names
`_DENYLIST_ENV_FRAGMENTS` as the env-var control; that symbol no longer
exists. `sandbox.py`'s `run_isolated`/`_child_entry` now rebuild the child's
environment from `_ALLOWLISTED_ENV_KEYS` (`PATH`, `HOME`, `TMPDIR`, `LANG`,
`LC_ALL`, `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`) -- a strictly stronger
control than the denylist section 3 describes: an allowlist cannot miss a
credential-shaped variable name the way a denylist substring match can
(the module's own docstring cites this as the reason for the change:
`APP_ENCRYPTION_KEY` and `ATTESTATION_SIGNING_KEY` are exactly the shape a
denylist keyed on `"API_KEY"`/`"SECRET"`/`"TOKEN"`/`"PASSWORD"`/
`"CREDENTIAL"`/`"MONGO_URL"` would miss). `CapabilityBroker`
(`control/execution_validators.py`, Phase 9 item 3) adds a second,
independent check at the capability-grant layer: an `ExecutionTask`'s
`CapabilityGrant` may never list a credential-shaped tool name
(`*_key`/`*_secret`/`*_token`/`*_password`/`*_credential`). Together these
confirm the criterion is met: no credential reaches the worker through its
environment, and none is granted to it as a capability.

This says nothing new about the residual risk section 3 already documents
in full and this confirmation does not repeat: the worker is a same-uid
child process with no `setuid`, no separate service account, and no
container/namespace boundary, so it can still open `backend/.env`,
`~/.aws/credentials`, `~/.ssh/`, or any other file the parent process's uid
can already read, directly by path, regardless of what its environment or
capability grant carry. `PathPolicyValidator` (Phase 9 item 3) confines
which paths the *rest of the codebase* is willing to hand the worker
(dataset files under `phi_core.paths.DATA_DIR`) -- exactly like
`validate_sandbox_path` already did per section 3 -- and, exactly as section
3 already concludes, this constrains callers, not what the worker's own
code could choose to open once running as that uid. The no-container
architectural decision this codebase has made leaves full process-identity
isolation undeliverable; this residual risk is disclosed, not remediated,
and stays open by the same design section 3 already records it under.

## 8. `SECURITY_BOUNDARY_VIOLATION` handling (spec section 71)

`control/security_incident.py` implements the durable handling spec section 71
requires: a `SecurityIncident` (a `ControlRecord`) is opened the moment
`ProviderGateway`'s live leak-canary-hit branch fires (see section 9 below), and is
deliberately typed with no field capable of holding the raw leaked value itself -- the
record documents that an incident happened, not what leaked. An open incident:

- **Blocks release**: `derive_security_incident_active` reads the durable
  `security_incidents` collection and feeds `FinalAssuranceGate`'s
  `no_unresolved_security_incident` condition, which returns `BLOCKED` while any
  incident for the run is open.
- **Does not auto-resume**: nothing clears an incident on its own.
  `resolve_security_incident` requires an explicit, authorized principal call.

**This condition's correctness does not close the export gap in section 10 below.**
`no_unresolved_security_incident` is a real, correctly-implemented condition inside
`evaluate_final_assurance` -- but `evaluate_final_assurance` itself has no live caller
in the current bundle/export path, so this condition, like every other
`FinalAssuranceGate` condition, does not actually run before a session is downloaded
today. What still runs regardless of the gate: `ProviderGateway`'s own
leak-canary-hit branch opens the incident record live, independent of whether
`FinalAssuranceGate` is ever invoked. The detection and the durable record are real;
only the release-blocking consequence tied to `FinalAssuranceGate` is currently
dormant on the live export path.

## 9. Leak-canary harness (`control/canary.py`, Wave R-d, spec section 72)

A run-scoped, in-process detection layer, separate from the sandbox controls in
sections 1-4: `activate_canary_set` plants literal strings tied to a run's ground
truth, and a scan of an outbound payload raises `SecurityBoundaryViolation` -- a
plain `Exception`, not a typed-hierarchy subclass, so it cannot be silently caught by
exception handling written for an unrelated error family -- the instant a planted
literal appears. The registry lives only in a module-level, process-local dict
(`_ACTIVE`) for the interval between `activate_canary_set()` and
`deactivate_canary_set()`: never a `ControlStore` collection, never serialized to
disk, never logged.

The harness covers 13 live surfaces: exports, plus 8 non-export surfaces
(`trace_events`, `workflow_runs.opaque_map`, agent logs, `HandoffEnvelope` payloads,
the learning store, research queries, errors, and ZIP metadata). A hit on any of
these surfaces is what feeds the `SECURITY_BOUNDARY_VIOLATION` incident described in
section 8 above.

## 10. The live export path does not pass through `FinalAssuranceGate` (open,
disclosed residual risk)

**This is the most important accuracy point in this document.** The live download
endpoints (`session_bundle`, `session_export`, `session_reversal_key`,
`session_acknowledge`, `session_cleanup_status`) gate on session status
(`complete`/`partially_complete`), an `EXPORT_RETENTION_WINDOW_DAYS` (default 14)
410-Gone check once elapsed, and `export_expires_at` surfaced on session reads. The
bundle bytes themselves are assembled by `phi_core/bundle.py::build_bundle`, a
self-contained ZIP assembler with its own coverage rendering, its own attestation
payload, its own README/methods/results/discussion generation, and its own SHA-256
hashing.

**`build_bundle` is not gated by `FinalAssuranceGate`.** `control/final_assurance.py`'s
`evaluate_final_assurance` is the master-architecture-mandated (section 57),
fifteen-condition "deterministic non-bypassable release gate... model confidence
cannot override it" -- and it has zero live call sites anywhere in `server.py`,
`superorchestrator.py`, or `agents/` (verified directly against the current
`server.py`'s bundle/export handlers: `build_bundle` is imported and called there,
`evaluate_final_assurance` is not referenced at all). This was disclosed
deliberately, not discovered late: the gate was built and extensively tested but
never force-wired into the download path, to avoid a regression risk across the
~100+ existing download tests, and remains open today. The same applies to
`ReportGenerator`/`ZIPBuilder`/`IntegrityService` -- a separate, fully built,
gate-verified report-pipeline cluster that is downstream of this same gap and is
also not part of the live export path.

**What this means in practice:** today, a session that reaches `complete` or
`partially_complete` can be downloaded through `build_bundle` without ever passing
the 15-condition `FinalAssuranceGate` check -- including the
`no_unresolved_security_incident` condition described in section 8 above.
`build_bundle` is not undefended: Publish Guard's deterministic release-content scan
is real and wired into the pipeline before a session can reach `complete` in the
first place, and the leak-canary detection in section 9 runs independently of
`FinalAssuranceGate`. But the specific mandatory-per-spec, non-bypassable release
gate -- the one condition set the architecture names as the backstop that "model
confidence cannot override" -- is simply not in the call path a real download
request takes. Treat this as an open, unresolved item: a deployment that requires
`FinalAssuranceGate`'s full condition set as an actual precondition for release
needs to wire `evaluate_final_assurance` into the bundle/export handlers before that
guarantee is real, not just tested.
