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

Codex Replacer 1.6 runs the privileged MCP server as its own persistent, loopback-only HTTP service on `127.0.0.1:8791/mcp`. The OpenAI tunnel client connects to that URL instead of owning `server.py` as one shared stdio child.

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

The main Codex Replacer VM is intentionally treated as a latency-sensitive control plane. Expensive transferable repo work should run in the full-VM lab instead of competing with every chat for the control VM's 8 vCPUs. Four full VMs are now prewarmed so heavy jobs do not pay the roughly 20-second on-demand creation path for stations 3-4.

### Control-VM runtime tuning

The live Codex Replacer VM is tuned for interactive latency as well as throughput:

- libvirt memory target: **24 GiB** (live and persistent)
- next-boot CPU target: **12 vCPU**; the current boot remains at 8 vCPU to avoid interrupting active chats
- `vm.swappiness=10`; stale swap from the previous 16-GiB allocation was cleared after the memory increase
- root ext4 uses `noatime` and does **not** use continuous `discard`
- `fstrim.timer` remains enabled for weekly batched trim instead of paying discard/unmap overhead during delete-heavy builds
- the VM root/work qcow2 files are currently backed by the homeserver's 7-disk spinning RAIDZ1 `tank` pool. Guest virtio scheduling (`none`), qcow2 `cache=none`, ZFS LZ4 and `atime=off` are already appropriate. Sampled pool wait under write activity was roughly 15–46 ms, so an SSD/NVMe tier is the largest remaining storage-performance opportunity. A maintenance-window migration from qcow2-on-ZFS files to raw ZFS zvols could reduce double-CoW overhead, but cannot remove the physical HDD seek latency.

## Codex computer lab

For lightweight parallel product work, Codex Replacer exposes a small leased workstation pool backed by isolated Docker containers on the control VM. CPU-heavy work should use the separate full-KVM lab on the parent homeserver described below, so expensive builds do not compete with interactive MCP traffic for the control VM's 8 vCPUs.

Current defaults:

- 4 prewarmed workstations
- 6 maximum active workstations on the current 8-vCPU / 16-GiB Codex VM
- 2 vCPU and 2 GiB RAM limit per workstation
- workspaces stored on `/tank/codex-lab`
- current human-readable sign-in sheet: `/tank/codex-lab/SIGN-IN-OUT.md`
- append-only audit log: `/tank/codex-lab/sign-in-out.jsonl`
- released/expired workspaces are archived under `/tank/codex-lab/archives` instead of being deleted
- leases default to 180 minutes and are capped at 24 hours

MCP workflow:

1. `lab_acquire` with the agent name and project.
2. `lab_exec` using the returned lease ID.
3. `lab_release` when work is complete.
4. `lab_list` shows who has each station and recent sign-in/out activity.
5. `lab_gc` releases expired leases and restores the prewarmed pool.

`codex-lab-gc.timer` runs maintenance every 15 minutes and also repopulates the prewarmed pool after boot.

The worker image is built from `lab/Dockerfile` as `codex-lab-worker:bookworm` and includes Node.js, Python, Git/GitHub CLI, build-essential, ripgrep, rsync, curl, and common archive utilities. Agents can use root inside their leased container when a project needs extra packages without modifying the main Codex VM.

To apply the durable service settings on a Codex VM:

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

The tunnel profile must use `mcp.server_urls` with `channel: main` and `url: http://127.0.0.1:8791/mcp`, then `codex-replacer.service` can be restarted. Keep the profile credential reference unchanged.

### Full KVM computer lab

For heavier or riskier work, Codex Replacer also exposes a full-VM lab backed by libvirt on the parent homeserver:

- `vm_lab_list` — show full VM stations and the sign-in/out ledger.
- `vm_lab_acquire` — lease a clean VM to an agent/project.
- `vm_lab_exec` — run work in the leased VM.
- `vm_lab_release` — sign out and normally recycle the VM from the golden image.
- `vm_lab_gc` — release expired leases and maintain the prewarmed pool.

The current default is **4 prewarmed VMs / 4 maximum**, each **4 vCPU, 8 GiB RAM, 100 GiB thin disk**, using `192.168.122.230` through `.233`. All four stations stay prewarmed; recycled sign-out restores a clean VM from the golden image and brings it back ready for the next agent. The golden image includes Git, GitHub CLI, Python, Node 24, npm, Docker, qemu-guest-agent, sudo, and `/workspace`.

The full-VM ledger is `/tank/codex-lab-vm/SIGN-IN-OUT.md`; its append-only audit log is `/tank/codex-lab-vm/sign-in-out.jsonl`. VM provisioning on the homeserver is handled by `lab/vm-labctl.sh` and `/tank/vm/codex-lab/vm-labctl.sh`.
