"""Shared fixtures; expensive resources are only created by requesting tests."""

pytest_plugins = ("tests.support.postgres",)
