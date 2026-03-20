"""Failure classes for browser work; each class decides what the task does next."""


class BrowserError(Exception):
    """Base for failures raised while driving the browser."""


class RecoverableError(BrowserError):
    """Class 1: transient or caused by a stale view of the page; re-observe and try again."""


class ElementNotFoundError(RecoverableError):
    pass


class ElementAmbiguousError(RecoverableError):
    pass


class ActionNotVerifiedError(RecoverableError):
    """The page did not show the effect the action should have had."""


class NeedsUserError(BrowserError):
    """Class 3: the site wants a login, MFA, CAPTCHA or bot check that only the owner may do."""


class IrrecoverableError(BrowserError):
    """Class 6: retrying will not help (unreachable site, blocked destination, repeated crashes)."""


class UnsafeDestinationError(IrrecoverableError):
    """The URL points at a non-http scheme or at a private / loopback / link-local address."""


class SecretLeakError(IrrecoverableError):
    """An action would have put a stored secret somewhere it must not go."""


class PictureRefusedError(BrowserError):
    """A screenshot would capture a sign-in form or a value from the secret store."""
