"""Nerva Core: the agent runtime that reads workspace context and decides what
to propose.

Milestone 4 adds its first real code: `rules` (turning a completed
`ConversationAnalysis` into a deterministic, inert business-signal effect — see
`nerva.nerva_ai` for why the LLM's own output is never trusted to decide this
directly) and `signal_lifecycle` (the business-signal status state machine,
mirroring `nerva_actions.lifecycle`). Coordinating broader agent behavior beyond
this one rule is still future work.
"""
