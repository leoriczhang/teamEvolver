"""Compatibility import for pull-based Session ingestion routes."""

from session_ingestion.pull.routes import register_pull_routes

register_datasource_routes = register_pull_routes

__all__ = ["register_datasource_routes", "register_pull_routes"]
