"""A fake `ActionExecutor` (`nerva.nerva_actions.executor`).

No real executor exists yet (Milestone 5 Block 1 defines the contracts only), so this is
the only implementation of the protocol — used to exercise it, and to stand in for the
future execution engine's dependency the same way `tests.fakes.ai.FakeAIClient` stands in
for `AIAnalysisClient`.
"""

from nerva.core.enums import ActionType
from nerva.nerva_actions.errors import ActionExecutionError
from nerva.nerva_actions.executor import ActionExecutionInput, ActionExecutionResult


class FakeActionExecutor:
    """Scripted responses standing in for a real provider adapter. Set `next_result` to
    control what `execute()` returns, or `fail_with` to make it raise instead. Every call
    is recorded in `calls`, so a test can assert how many times (and with what input) the
    "provider" was actually invoked — the basis of future idempotency tests."""

    def __init__(
        self, action_type: ActionType, *, result: ActionExecutionResult | None = None
    ) -> None:
        self.action_type = action_type
        self.next_result = result or ActionExecutionResult()
        self.fail_with: ActionExecutionError | None = None
        self.calls: list[ActionExecutionInput] = []

    def execute(self, execution_input: ActionExecutionInput) -> ActionExecutionResult:
        self.calls.append(execution_input)
        if self.fail_with is not None:
            raise self.fail_with
        return self.next_result

    @property
    def call_count(self) -> int:
        return len(self.calls)
