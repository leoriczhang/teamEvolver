"""Mining business policies with injected authoritative metadata storage."""

from .core_repository_part0 import RepositoryCoreMixin
from .core_repository_part1 import RepositoryPart1Mixin
from .core_repository_part2 import RepositoryPart2Mixin


class OvSkillMiningRepository(RepositoryCoreMixin, RepositoryPart1Mixin, RepositoryPart2Mixin):
    """Sources/jobs live in the required backend; project_root is disposable cache."""
