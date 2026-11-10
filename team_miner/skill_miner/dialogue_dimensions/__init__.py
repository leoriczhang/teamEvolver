"""Bounded analysis of selected excellent dialogues, independent of Skill publication."""

PROTOCOL = "te-dialogue-dimensions.v1"


def is_dialogue(job):
    return job.get("execution_protocol") == PROTOCOL
