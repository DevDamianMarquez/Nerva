"""Real `AIAnalysisClient` implementations, one per vendor SDK.

Only modules in this package may import a vendor AI SDK — everything else in
Nerva depends on `nerva_ai.client.AIAnalysisClient`, the same discipline
`nerva_connect.google` applies to Google's SDKs (see docs/ARCHITECTURE.md).
"""
