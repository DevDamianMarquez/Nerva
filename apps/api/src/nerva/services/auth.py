"""Signup, login and session verification.

This answers "who is this user?" only. Whether that user can access a given workspace
is a separate question, answered by `WorkspaceService.get_context` from
`workspace_members` (see `nerva.services.identity`).
"""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.errors import AuthenticationError, ConflictError
from nerva.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from nerva.models import User
from nerva.repositories.identity import SessionRepository, UserRepository


class AuthService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.sessions = SessionRepository(session)

    def signup(self, *, email: str, name: str, password: str) -> tuple[User, str]:
        if self.users.get_by_email(email) is not None:
            raise ConflictError("A user with this email already exists.")
        user = User(email=email, name=name, password_hash=hash_password(password))
        try:
            self.users.add(user)
            token = self._issue_session(user.id)
            self.session.commit()
        except IntegrityError as exc:  # concurrent signup with the same email
            self.session.rollback()
            raise ConflictError("A user with this email already exists.") from exc
        return user, token

    def login(self, *, email: str, password: str) -> tuple[User, str]:
        user = self.users.get_by_email(email)
        # Checking the password against a fixed hash when the user does not exist would
        # avoid a timing signal, but bcrypt's own cost already dominates the timing here;
        # the message stays identical either way so a wrong email cannot be told apart
        # from a wrong password.
        if user is None or not verify_password(password, user.password_hash):
            raise AuthenticationError("Incorrect email or password.")
        token = self._issue_session(user.id)
        self.session.commit()
        return user, token

    def authenticate(self, token: str) -> User | None:
        record = self.sessions.get_active(hash_session_token(token))
        if record is None:
            return None
        return self.users.get(record.user_id)

    def logout(self, token: str) -> None:
        self.sessions.revoke(hash_session_token(token))
        self.session.commit()

    def _issue_session(self, user_id: uuid.UUID) -> str:
        token = generate_session_token()
        self.sessions.add(user_id=user_id, token_hash=hash_session_token(token))
        return token
