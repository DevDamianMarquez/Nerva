"""Google OAuth and Gmail: the only place that speaks Gmail's API shapes.

`oauth.py` and `gmail.py` know nothing about workspaces, sessions or SQLAlchemy; they
take and return plain values (tokens, dataclasses) so they can be exercised without a
database, and swapped for fakes in tests without a real Google account. `mapper.py`
turns a raw Gmail message into the plain, already-normalized shapes the rest of Nerva
uses (`Conversation` / `Message` fields) — orchestrating that mapping into actual
database rows is `nerva.services.integrations`, not this package.
"""
