"""Offline collaborators for assembling the complete HTTP route surface."""

from types import SimpleNamespace

from teamEvolver.app import build_role_services
from teamEvolver.config import TeamEvolverConfig


def composition_services():
    tx = SimpleNamespace(pool=SimpleNamespace(schema="te"))
    return build_role_services(role="api", tx=tx, config=TeamEvolverConfig())
