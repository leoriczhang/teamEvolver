"""Worker registration, with each mining business owning its handlers."""


def register_handlers():
    from .knowledge_miner.handlers import register_handlers as knowledge_handlers
    from .skill_miner.handlers import register_handlers as skill_handlers

    return {**knowledge_handlers(), **skill_handlers()}
