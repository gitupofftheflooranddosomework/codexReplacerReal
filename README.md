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
python3 -m py_compile codex-replacer/server.py codex-replacer/smoke-test.py
python3 codex-replacer/smoke-test.py
systemd-analyze --user verify codex-replacer/systemd/codex-chatgpt-browser.service
```
