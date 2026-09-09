# SlackController chat-worker bridge

SlackController is shared chat-worker infrastructure. It is not part of DotMoose or any other project application, and using it never requires changes to a project repository or calls to that project's own Slack bots. Project/repository names are routing metadata only.

After a chat worker acquires its own full KVM with `vm_lab_acquire`, it uses that lease ID with the first-class broker tools:

- `slackcontroller_identity` — resolve the current worker identity from its active lease/IP; no logical handle is accepted.
- `slackcontroller_inbox` — non-destructively read only that worker's routed messages.
- `slackcontroller_reply` — reply to one routed message by message ID; channel/thread routing remains bound inside SlackController.
- `slackcontroller_ack` — acknowledge only that worker's selected message IDs, preserving other broadcast recipients.
- `slackcontroller_heartbeat` — set only that worker's `active`, `idle`, or `offline` state.

The broker validates the lease and performs the fixed private HTTP request inside the leased KVM, so SlackController sees the worker's actual `192.168.122.x` TCP peer and validates it against the homeserver lease state. Callers cannot supply a bot handle, Slack channel ID, thread timestamp, arbitrary URL, headers, API path, or the master SlackController bearer token.

Default private worker API: `http://10.0.0.181:8788/worker/v1/`. Operators may override the fixed base with `SLACKCONTROLLER_WORKER_BASE_URL` on the Codex Replacer service; chat callers cannot override it.
