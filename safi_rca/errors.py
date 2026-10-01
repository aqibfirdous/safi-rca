class SafiRcaError(Exception):
    """Base class for every error raised by safi-rca."""


class GitError(SafiRcaError):
    """A git plumbing command failed."""


class InputError(SafiRcaError):
    """The caller supplied an unusable repo / sha / evidence path."""


class ReadOnlyViolation(SafiRcaError):
    """The analyzed repository changed during analysis."""


class RuntimeUnavailable(SafiRcaError):
    """The requested agent runtime cannot be used in this environment."""


class RuntimeError_(SafiRcaError):
    """The agent runtime failed to produce a report."""
