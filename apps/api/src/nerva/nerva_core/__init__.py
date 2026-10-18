"""Nerva Core: the agent runtime that reads workspace context and decides what
to propose.

Milestone 4 adds its first real code: `rules` (turning a completed
`ConversationAnalysis` into a deterministic, inert business-signal effect — see
`nerva.nerva_ai` for why the LLM's own output is never trusted to decide this
directly) and `signal_lifecycle` (the business-signal status state machine,
mirroring `nerva_actions.lifecycle`). `memory_rules` (Milestone 6.3) adds the
one rule for *writing* an automated memory. `memory_context` (Milestone 6.4,
Block 1) adds the first rule for *reading* memory back for reasoning:
deterministic, bounded selection and ranking of already-active memories for a
future analysis context — selection only; nothing yet builds its inputs from
real rows or feeds its output into a prompt. Coordinating broader agent
behavior beyond these rules is still future work.
"""
