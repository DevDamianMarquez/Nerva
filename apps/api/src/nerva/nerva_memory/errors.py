"""Errors raised while validating a memory's content contract.

Kept separate from `nerva.core.errors` for the same reason `nerva_actions.errors`
is: this describes a failure at the content-contract boundary, not an HTTP-shaped
API error. A future route maps it to its own error response the same way
`routes.actions` already does for `nerva_actions.errors`.
"""


class MemoryContentError(Exception):
    """Raised when raw content does not satisfy `nerva_memory.content.MemoryContent`."""
