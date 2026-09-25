"""Nerva AI: the LLM-facing boundary that turns a conversation into a validated,
structured analysis — and nothing more.

This package never touches the database or a workspace, and never decides what
business object gets created from its output (see `nerva_core.rules` for that,
once it exists). It only knows how to take normalized conversation content and
return a `ConversationAnalysisResult` that has already been validated against a
strict schema, or raise a typed `AIError`. Provider-specific code (which vendor,
which SDK, which wire format) lives under `providers/` and is never imported
outside this package — the rest of Nerva depends only on `AIAnalysisClient`.
"""
