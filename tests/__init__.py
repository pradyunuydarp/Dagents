"""Repository-level tests that span more than one service.

Everything here needs more than one service's dependencies installed, which is
why it does not live under `agents/tests/` or in any single service's suite: the
service inventory is only meaningful when every Python service can be imported
and every Spring controller parsed. Keeping it out of `agents/tests/` also keeps
that suite runnable with only the agent requirements, as the contributor guide
says it is.

Run it from the repository root:

    .venv/bin/python -m unittest discover -s tests -t .
"""
