# codexReplacerReal

Private MCP operator for the Codex Replacer VM.

## Conversation continuity

Codex Replacer exposes `prepare_chat_handoff`. The MCP initialize instructions tell the model to use it proactively when a long conversation is approaching context risk, preserving the objective, current state, completed/pending work, blockers, constraints, exact references, and next actions.

`chatgpt_start_chat` can then seed the handoff into a new ChatGPT conversation and returns the saved `/c/...` URL only after it is confirmed.

## Headed ChatGPT browser bridge

ChatGPT account operations use a dedicated normal Chromium window on the VM desktop, not the general sandboxed/headless browser MCP:

- persistent profile: `~/.local/share/codex-replacer/chatgpt-browser`
- local-only Chrome DevTools endpoint: `127.0.0.1:9222`
- user service: `codex-chatgpt-browser.service`
- Playwright MCP attaches temporarily with `--cdp-endpoint http://127.0.0.1:9222`
- the headed browser is reachable only through purpose-built ChatGPT tools; raw headed-browser controls are not exposed to the model
- the existing general browser remains sandboxed and keeps its private-network egress restrictions

Install/reinstall the headed browser service with:

```sh
./codex-replacer/install-headed-chatgpt-browser.sh
```

The dedicated Chromium profile requires a one-time interactive ChatGPT sign-in. Authentication then persists in that profile across MCP/service restarts.

`chatgpt_browser_status` reports whether the headed browser is reachable and authenticated without creating a chat. `chatgpt_auth_begin` can initiate only user-approved `passkey` or `phone_prompt` authentication flows; it deliberately has no input for passwords, OTPs, backup codes, or MFA secrets.

The Chromium profile is mode `0700`, systemd linger is enabled where available, the CDP endpoint remains bound to `127.0.0.1`, and the service uses the same on-disk profile across browser/MCP restarts. `--password-store=basic` avoids tying persisted Chromium cookies to an interactive desktop keyring that may not be unlocked when the service starts after reboot.

## Why not FlareSolverr

FlareSolverr describes itself as a proxy for bypassing Cloudflare/DDoS-GUARD challenges and uses undetected-chromedriver. Codex Replacer does not use a challenge-bypass layer for ChatGPT. It operates a normal headed browser session belonging to the user instead, which is both more stable for an authenticated UI workflow and keeps anti-bot handling out of the MCP architecture.

## Verification

Run:

```sh
python3 -m py_compile codex-replacer/server.py codex-replacer/http-server.py codex-replacer/lab_manager.py codex-replacer/smoke-test.py codex-replacer/concurrency-test.py codex-replacer/http-concurrency-test.py codex-replacer/host-exec-promotion-test.py
python3 codex-replacer/concurrency-test.py
python3 codex-replacer/http-concurrency-test.py
python3 codex-replacer/host-exec-promotion-test.py
python3 codex-replacer/smoke-test.py
systemd-analyze --user verify codex-replacer/systemd/codex-replacer-mcp.service codex-replacer/systemd/codex-chatgpt-browser.service codex-replacer/systemd/codex-lab-gc.service codex-replacer/systemd/codex-lab-gc.timer
```

## Parallel MCP dispatcher

Codex Replacer 1.4 processes independent MCP requests concurrently instead of serializing every request behind one synchronous stdin loop.

- server default and hard maximum: **20 concurrent MCP requests**
- server override: `CODEX_REPLACER_MAX_WORKERS` (clamped to 20)
- tunnel dispatcher: `MCP_MAX_CONCURRENT_REQUESTS=20` so the tunnel does not impose its old 10-request ceiling
- control-plane buffer: `CONTROL_PLANE_MAX_INFLIGHT_REQUESTS=20`
- browser requests still serialize on the shared browser client lock so one persistent browser profile cannot be corrupted by overlapping automation
- normal shell, Git, GitHub, HTTP, filesystem, process, and lab work can execute concurrently
- default captured command output is 1 MiB; callers can explicitly request more when needed
- structured tool results are no longer duplicated in full as text content; the text compatibility preview is capped at 2 KiB
- each completed MCP request writes an `mcp_request_completed` timing record to the service journal with method, tool, status, and elapsed milliseconds

This separates server-side execution time from ChatGPT reasoning/tool-transport time when diagnosing slow turns.

### Isolated Streamable HTTP transport

Codex Replacer 2.0 runs the privileged MCP server as its own persistent, loopback-only HTTP service on `127.0.0.1:8791/mcp`. The OpenAI tunnel client connects to that URL instead of owning `server.py` as one shared stdio child.

This matters under many simultaneous chats: expiring or abandoning one MCP connection no longer closes the stdin/stdout pipes underneath every other chat. The tunnel can reconnect independently while the local MCP service, background process sessions, and unrelated HTTP requests remain alive.

- local MCP service: `codex-replacer-mcp.service`
- endpoint: `http://127.0.0.1:8791/mcp`
- health: `http://127.0.0.1:8791/healthz`
- the privileged endpoint binds only to loopback and rejects non-loopback browser origins
- HTTP requests are handled concurrently; the MCP/tunnel concurrency ceiling remains 20
- tunnel MCP connection maximum TTL is 30 minutes, but correctness no longer depends on keeping one connection alive indefinitely
- commands expected to exceed about 60 seconds should normally use `process_start` / `process_poll` instead of a synchronous `host_exec` request
- mutating calls are never assumed safe to retry after an uncertain transport failure; inspect state first because the operation may already have completed
- foreground `host_exec` has a **20-second interactive budget**; if a command is still running, it is kept alive as a managed process and the tool returns a `sessionId` immediately for `process_poll` instead of blocking the chat
- `host_exec` commands containing an early `sleep` of 3 seconds or more are promoted immediately instead of burning a foreground request waiting for a timer
- managed host commands run at nice level 5 so CPU-heavy builds/scans do not starve the MCP/tunnel control path when many chats are active
- visual-browser tool discovery is prewarmed in the background when the persistent MCP service starts, removing the roughly one-second cold browser-tool setup from the first browser-heavy chat
- agents are instructed to reuse/close browser tabs so long-lived Chromium profiles do not grow unbounded and consume shared memory/process slots

`http-concurrency-test.py` runs eight one-second commands in parallel and deliberately abandons a long HTTP request; a second request must still complete while the abandoned command is running.

The main Codex Replacer VM is intentionally treated as a latency-sensitive control plane. Expensive transferable work is submitted to the homeserver scheduler, which distributes it over six persistent KVM workers so the controller stays responsive even when many chats are active.

### Control-VM runtime tuning

The live Codex Replacer VM is tuned for interactive latency as well as throughput:

- libvirt memory target: **24 GiB** (live and persistent)
- next-boot CPU target: **12 vCPU**; the current boot remains at 8 vCPU to avoid interrupting active chats
- `vm.swappiness=10`; stale swap from the previous 16-GiB allocation was cleared after the memory increase
- root ext4 uses `noatime` and does **not** use continuous `discard`
- `fstrim.timer` remains enabled for weekly batched trim instead of paying discard/unmap overhead during delete-heavy builds
- the VM root/work qcow2 files are currently backed by the homeserver's 7-disk spinning RAIDZ1 `tank` pool. Guest virtio scheduling (`none`), qcow2 `cache=none`, ZFS LZ4 and `atime=off` are already appropriate. Sampled pool wait under write activity was roughly 15–46 ms, so an SSD/NVMe tier is the largest remaining storage-performance opportunity. A maintenance-window migration from qcow2-on-ZFS files to raw ZFS zvols could reduce double-CoW overhead, but cannot remove the physical HDD seek latency.

## Codex computer lab

Codex Replacer 2.1 uses a **six-computer persistent KVM lab** as its primary parallel-work backend. The older Docker workstation pool still exists for lightweight compatibility/on-demand isolation, but it is not prewarmed and should not be used for CPU-heavy work.

### Six persistent KVM computers

The parent homeserver keeps six full KVM workstations powered on and ready:

| Station | VM | IP | Direct browser |
|---:|---|---|---|
| 1 | `codex-lab-vm-01` | `192.168.122.230` | `https://browser1.home.markshaw.ca/` |
| 2 | `codex-lab-vm-02` | `192.168.122.231` | `https://browser2.home.markshaw.ca/` |
| 3 | `codex-lab-vm-03` | `192.168.122.232` | `https://browser3.home.markshaw.ca/` |
| 4 | `codex-lab-vm-04` | `192.168.122.233` | `https://browser4.home.markshaw.ca/` |
| 5 | `codex-lab-vm-05` | `192.168.122.234` | `https://browser5.home.markshaw.ca/` |
| 6 | `codex-lab-vm-06` | `192.168.122.235` | `https://browser6.home.markshaw.ca/` |

Each worker is a **full Debian 12 Linux workstation** sized at **4 vCPU, 8 GiB RAM and a 100 GiB thin qcow2 disk**. The workers are persistent computers rather than browser appliances or disposable containers. They expose normal SSH/shell execution to agents, keep `/workspace` and build caches across ordinary release/reacquisition, and run Docker locally. The workstation image includes Git/Git LFS, GCC/G++, Clang/GDB, CMake/Ninja, Python/pip/venv/pipx, the existing Node/npm runtime, Go, Rust/Cargo, Java, PHP/Composer, Docker, database clients, tmux/screen, shell/network/debug tooling, plus a visible terminal/file-manager desktop layer. Chromium/noVNC is only one capability of the workstation.

The browser/workstation golden image is `codex-lab-base-browser-v2.qcow2`. `lab/worker-workstation/bootstrap.sh` is the idempotent workstation provisioning source used for both the current six VMs and future golden-image rebuilds. The visible Openbox desktop includes a lightweight panel and launchers for a terminal, file manager, and Chromium. A clone derives its hostname from the deterministic station MAC and generates fresh SSH host keys on first boot, so provisioning a new station is normally only **qcow2 overlay + virt-install + boot** rather than a per-VM guestfs customization pass.

### Watch all six computers

`https://browser.home.markshaw.ca/` is the authenticated six-screen operations dashboard. It updates every two seconds and has three layers: **scheduler health/throughput**, **per-VM identity/status/controls**, and the six live noVNC desktops. Every VM card shows CPU, RAM, root-disk usage, uptime, Linux/Docker/browser readiness, active job or interactive lease, elapsed time, and the stable `owner` bot/agent name. Scheduled jobs and interactive leases can also carry `chatLabel` and `chatUrl`; when a real `https://chatgpt.com/...` URL is supplied, the VM card exposes an **open chat** link. The MCP tunnel does not expose a ChatGPT conversation ID automatically, so agents are instructed to pass real chat metadata when available and never invent it. The dashboard uses its own HTML login page rather than HTTP Basic Auth: username `mark`, a salted PBKDF2-SHA256 password verifier stored in `/tank/vm/codex-lab/dashboard-auth.json` with mode `0600`, 12-hour signed sessions in a host-only `Secure; HttpOnly; SameSite=Strict` cookie, login throttling, and a built-in password-change page. Password changes rotate the session secret and invalidate other sessions. Plaintext passwords are never stored in the auth file.

The legacy direct hostnames `browser1.home.markshaw.ca` through `browser6.home.markshaw.ca` and `browser-controller.home.markshaw.ca` only redirect to canonical paths on `browser.home.markshaw.ca`. This keeps the auth cookie scoped to one hostname instead of sending it to unrelated `*.home.markshaw.ca` services. For administrative recovery, `lab/reset-dashboard-auth.py --generate` can reset the `mark` login and invalidate all existing sessions if the password is forgotten again.

Each VM card has direct operational controls: **Desktop**, **Terminal** (launches an `xterm` on that VM's visible desktop), **Job logs** for the active scheduled job, **Restart browser**, **Cancel job**, and **Release lease**. Mutating dashboard actions are POST-only and require the signed dashboard session plus its CSRF token. Job-log responses are deliberately redacted to owner/project/chat/status/timestamps plus stdout/stderr; stored commands and environment variables are not returned to the human dashboard endpoint.

The scheduler metrics panel shows loop health/age/error count, 5-minute jobs/minute throughput, queue-delay average/p95, 1-hour success rate, six-worker utilization, runtime average/p95, oldest queued age, and a live inline **SVG** history covering the last 30 minutes. The SVG renders completed jobs/minute as bars, failed jobs as an overlay, and average queue delay as a line. Metrics are derived from the existing SQLite job history, so they survive scheduler restarts; process-loop health is live runtime state.

The dashboard and worker views are protected by the scheduler's signed-session login gate through Caddy `forward_auth`; there is no HTTP Basic Auth popup. The noVNC/CDP endpoints themselves stay on the private libvirt network; CDP remains loopback-only inside each worker and controller automation reaches it through per-worker SSH tunnels. The central scheduler listens only on `192.168.122.1:8766` and is not directly exposed on the home LAN. The Caddy route source is retained as `lab/Caddyfile.browser-login.snippet` so the live proxy configuration can be reconstructed without storing credentials.

For interactive browser automation, an agent first calls `vm_lab_acquire` with its stable agent name. It then supplies the returned `station` and `leaseId` to the `vm_browser_*` tools. There are 26 KVM-browser actions mirroring the normal browser toolset (`vm_browser_navigate`, `vm_browser_snapshot`, `vm_browser_click`, `vm_browser_type`, screenshots, tabs, mouse actions, etc.). Each station has its own persistent browser-MCP/CDP connection, so **six independent visible browser agents can operate concurrently**. The exclusive lease prevents two agents from clicking or typing in the same worker at once.

Ordinary sign-out uses `vm_lab_release` without reimaging. `recycle=true` is reserved for the cases where a genuinely clean workstation is required.

### Central job scheduler

Long or CPU-heavy transferable work should use `vm_job_submit` instead of running on the controller. When two or more independent heavy tasks exist, agents should use `vm_job_submit_batch`; dispatch is automatic, so agents do not pick station numbers and one batch can fill all six KVMs in a single MCP round trip. The scheduler is a persistent homeserver user service:

- service: `codex-lab-scheduler.service`
- listener: `192.168.122.1:8766` on the private libvirt network
- database: `/tank/vm/codex-lab/scheduler.sqlite3`
- source: `lab/scheduler.py`
- one heavy scheduled job per KVM worker at a time
- FIFO queue when all six workers are busy
- least-recently-used free-worker selection
- transient per-job systemd units on the worker at nice level 5 / CPUWeight 80
- durable stdout/stderr and exit status under the worker's `~/.local/share/codex-worker/jobs/`
- persistent per-worker Git mirror cache so repeated repository jobs avoid full network/object downloads
- reciprocal lock files so scheduler jobs and exclusive interactive/browser leases can never collide on the same worker

MCP scheduler tools:

- `vm_job_submit` — enqueue one build/test/scan/other expensive command and return immediately with `jobId`; the caller never chooses a VM.
- `vm_job_submit_batch` — submit up to 48 independent heavy jobs in one MCP call; the scheduler immediately fills all free workers (up to all six) and queues the remainder.
- `vm_job_status` — read state and recent stdout/stderr; use this to poll instead of resubmitting work.
- `vm_job_list` — inspect recent jobs across all six workers.
- `vm_job_cancel` — cancel queued/running work.
- `vm_worker_status` — show all six workers, CPU/load/memory/disk, workstation/Docker/browser readiness, active job/lease ownership, scheduler health/throughput metrics, and browser URLs.

Attribution fields on `vm_job_submit`, `vm_job_submit_batch`, and `vm_lab_acquire` are `owner` (required stable bot name), `project`, optional `chatLabel`, and optional `chatUrl`. The exact chat URL is intentionally optional because current MCP requests do not carry a trustworthy ChatGPT conversation ID automatically.

When work is tied to a repository, pass `repoUrl` and `revision` whenever practical. The worker maintains a mirror cache and creates an isolated checkout for that job. GitHub API/PR actions can remain on the controller; the expensive compiler/test/build workload belongs on a worker.

The scheduler is deliberately independent of the OpenAI tunnel/MCP process. That means a future second or third controller can point at the same scheduler rather than introducing another incompatible worker state database. The present controller is kept single because its 20-request tunnel/MCP queue has remained empty during scheduler load tests; add another controller only when measured control-plane saturation justifies it.

### Storage parallelism

The homeserver has three independent spinning-disk ZFS pools. To avoid making six worker CPUs wait on one RAIDZ queue, KVM storage is striped operationally across the pools with **two workers per pool**:

- stations 1 and 4: `/tank/vm/codex-lab-v2`
- stations 2 and 5: `/tank2/vm/codex-lab`
- stations 3 and 6: `/tank3/vm/codex-lab`

Each pool keeps a local copy of the browser-capable golden image, so a worker overlay reads its base from the same physical pool as its writable qcow2. This does not turn HDDs into SSDs, but it gives concurrent builds three independent disk queues instead of one. An SSD/NVMe VM tier remains the largest possible future storage improvement.

### Lightweight compatibility lab

The Docker lab (`lab_acquire`, `lab_exec`, `lab_release`) remains available for small isolated tasks. Its prewarm count is **0** by default under v2.0; this frees controller CPU/RAM for the OpenAI/MCP/browser-control path. Existing `/tank/codex-lab` workspaces/archives are retained.

### Verification

Core controller verification:

```sh
python3 -m py_compile codex-replacer/server.py codex-replacer/http-server.py codex-replacer/vm_lab_manager.py codex-replacer/vm_job_scheduler.py lab/scheduler.py
python3 codex-replacer/concurrency-test.py
python3 codex-replacer/http-concurrency-test.py
python3 codex-replacer/host-exec-promotion-test.py
python3 codex-replacer/smoke-test.py
python3 lab/scheduler-smoke-test.py --jobs 6 --sleep 2
python3 lab/scheduler-smoke-test.py --jobs 12 --sleep 2
```

To apply the durable controller service settings:

```sh
mkdir -p ~/.config/systemd/user/codex-replacer.service.d
cp codex-replacer/systemd/codex-replacer-mcp.service ~/.config/systemd/user/
cp codex-replacer/systemd/codex-replacer-performance.conf ~/.config/systemd/user/codex-replacer.service.d/20-performance.conf
cp codex-replacer/systemd/codex-replacer-http-transport.conf ~/.config/systemd/user/codex-replacer.service.d/30-http-mcp.conf
cp codex-replacer/systemd/codex-lab-gc.service ~/.config/systemd/user/
cp codex-replacer/systemd/codex-lab-gc.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-replacer-mcp.service codex-lab-gc.timer
```

The tunnel profile uses `mcp.server_urls` with `channel: main` and `url: http://127.0.0.1:8791/mcp`. Keep the profile credential reference unchanged.

The full-VM sign-in/out ledger remains `/tank/codex-lab-vm/SIGN-IN-OUT.md`, with append-only audit log `/tank/codex-lab-vm/sign-in-out.jsonl`. VM provisioning on the homeserver is handled by `lab/vm-labctl.sh` and `/tank/vm/codex-lab/vm-labctl.sh`.
