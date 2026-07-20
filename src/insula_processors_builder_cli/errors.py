class CliError(Exception):
    """User-facing error. The CLI prints its message and exits non-zero.

    Never put a secret (api token, PAT, registry password) in the message.
    """


class DispatchError(CliError):
    pass


class RunFailedError(CliError):
    pass


class ArtifactError(CliError):
    pass


class PublishError(CliError):
    pass
