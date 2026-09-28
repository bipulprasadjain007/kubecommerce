"""Repository-level tests against the SQLite test database."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI

from app import repository as repo
from app.repository import EmailAlreadyExists


async def test_create_and_fetch(running_app: FastAPI) -> None:
    async with running_app.state.session_factory() as session:
        created = await repo.create_user(
            session, email="repo@example.com", password_hash="hash", full_name="Repo"
        )
        assert created.id is not None

        found_by_email = await repo.get_by_email(session, "repo@example.com")
        assert found_by_email is not None
        assert found_by_email.id == created.id

        found_by_id = await repo.get_by_id(session, created.id)
        assert found_by_id is not None
        assert found_by_id.email == "repo@example.com"

        assert await repo.get_by_id(session, uuid.uuid4()) is None
        assert await repo.get_by_email(session, "missing@example.com") is None


async def test_duplicate_email_raises(running_app: FastAPI) -> None:
    async with running_app.state.session_factory() as session:
        await repo.create_user(
            session, email="duplicate@example.com", password_hash="hash", full_name=None
        )
        with pytest.raises(EmailAlreadyExists):
            await repo.create_user(
                session, email="duplicate@example.com", password_hash="hash", full_name=None
            )
