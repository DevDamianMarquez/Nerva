"""Nerva Connect: connectors to external tools.

As of Milestone 3, Gmail is read-only: OAuth and importing conversations/messages into
a workspace (`nerva_connect/google/`). Nothing here ever sends, modifies or deletes
anything at the provider — the rest of Nerva works with normalized `Conversation` /
`Message` rows, never provider-specific payloads, so a second connector (e.g. Outlook)
is an addition here, not a redesign elsewhere.
"""
