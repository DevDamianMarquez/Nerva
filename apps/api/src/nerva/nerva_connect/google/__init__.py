"""Google OAuth and Gmail: the only place that speaks Gmail's API shapes.

`oauth.py` and `gmail.py` know nothing about workspaces, sessions or SQLAlchemy; they
take and return plain values (tokens, dataclasses) so they can be exercised without a
database, and swapped for fakes in tests without a real Google account. `mapper.py`
turns a raw Gmail message into the plain, already-normalized shapes the rest of Nerva
uses (`Conversation` / `Message` fields) — orchestrating that mapping into actual
database rows is `nerva.services.integrations`, not this package.

`sender.py` is the Gmail `send_email` provider boundary: a `Protocol`
(`GmailSender`), a safe result shape, and error translation into
`nerva_actions.errors`. Block 4 designed the contract with no real implementation and
no OAuth scope change; Block 5 adds the real implementation (`GmailAPISender`, an
HTTPS `POST` to `users.messages.send`) and expands `oauth.py`'s `SCOPES` to include
`gmail.send` alongside `gmail.readonly`. `send_executor.py` (Block 5) is the real
`ActionExecutor` (`GmailSendExecutor`) that resolves a workspace's Google
`Integration`, obtains an access token through `nerva.services.integrations.
GoogleCredentialRefresher`, and calls `GmailSender` — see `docs/ARCHITECTURE.md`
§5.5.
"""
