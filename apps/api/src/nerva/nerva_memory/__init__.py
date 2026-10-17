"""Nerva Memory: the domain contracts for durable, provenanced workspace memory.

This package builds the contracts for how a memory is shaped, validated and
allowed to change state — plus, as of M6.2, how it is queried back out.
Nothing here decides *what* is worth remembering, reads a conversation
automatically, or calls an AI provider; that is Nerva's reasoning layer, in a
later block.

- `content`: `MemoryContent`, the structured, `extra="forbid"` contract every
  `Memory.content` must satisfy before it is written, and `parse_memory_content`,
  mirroring `nerva_actions.payloads`'s `parse_action_payload`.
- `lifecycle`: the status state machine (`active -> superseded | discarded`),
  mirroring `nerva_core.signal_lifecycle`.
- `errors`: `MemoryContentError`, raised by `content.parse_memory_content`.

`nerva.services.memory.MemoryService` is the one place a `Memory` row is
created, resolved or queried — it enforces these contracts, applies retrieval
policy (`list` defaults to `active`-only; see its docstring), and records the
activity trail, the same way `ActionService`/`SignalService` do for their own
domains. Ranking, automatic extraction from conversations, embeddings, and
wiring this into an autonomous agent are not built yet.
"""
