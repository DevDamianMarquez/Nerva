"""Nerva Connect: connectors to external tools.

Since Milestone 3, Gmail import (OAuth + read-only sync, `nerva_connect/google/`)
works with normalized `Conversation`/`Message` rows, never provider-specific
payloads, so a second connector (e.g. Outlook) is an addition here, not a redesign
elsewhere.

Milestone 5 adds Gmail `send_email`: Block 4 designed the provider-neutral
contract/boundary (`google/sender.py`) without making any write call; Block 5 adds
the real implementation (`GmailAPISender`, also in `sender.py`) and the real
`ActionExecutor` that drives it (`google/send_executor.py`, `GmailSendExecutor`);
Block 6 wires it into production (`nerva.api.deps.get_action_executors` builds the
real `ActionExecutorRegistry`, `POST .../actions/{id}/execute` — owner-only — calls
`ActionExecutionEngine` with it). See `docs/ARCHITECTURE.md` §5.5/§5.6. The
connected OAuth scope now includes `gmail.send` alongside `gmail.readonly` (see
`google/oauth.py`); an account connected before Block 5 must reconnect before it
can send (see `google/sender.ensure_send_scope_granted` and
`integration_has_send_scope`).
"""
