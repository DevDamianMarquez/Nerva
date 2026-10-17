"""Nerva Actions: how proposed actions are approved and executed.

- `lifecycle`: the status state machine (`proposed -> approved -> executing ->
  completed | failed`, or `proposed -> rejected`).
- `payloads`: strict, per-type contracts for `Action.payload` (`send_email`,
  `update_opportunity_stage`), and `parse_action_payload`.
- `executor`: the provider-neutral `ActionExecutor` protocol and its typed
  input/result — implemented by a provider adapter (e.g. `GmailSendExecutor`), never
  by the domain layer.
- `eligibility`: the pre-flight check (status + payload + reference completeness)
  `engine.ActionExecutionEngine` runs before calling `ActionService.start_execution`.
- `errors`: the error hierarchy raised by the above.
- `engine`: `ActionExecutionEngine`, the service that resolves an `ActionExecutor` for an
  approved action's type and drives it through `ActionService`'s lifecycle methods to
  `completed`/`failed`.
- `opportunity_stage_executor`: `UpdateOpportunityStageExecutor`, the first real
  `ActionExecutor` — internal-only, provider-free (see `docs/ARCHITECTURE.md` §5.3).

The real `send_email` `ActionExecutor` (`GmailSendExecutor`, Milestone 5 Block 5) does
**not** live in this package: it depends on `nerva.nerva_connect` and `httpx`, which
this package must never import (see `test_nerva_actions_package_never_imports_google_
or_http_transport_modules` in `tests/test_gmail_send_boundary.py`) — it lives in
`nerva.nerva_connect.google.send_executor` instead, with its provider client, the same
way `nerva_ai`'s real provider adapter lives in `nerva_ai/providers/`, not in the
domain layer. No executor for `create_task` exists yet.
"""
