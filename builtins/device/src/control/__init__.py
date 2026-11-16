"""Screen control: the on-screen indicator, screenshots, mouse and keyboard.

Nothing in here acts on the machine until :class:`~src.control.session.ControlSession`
has been started, and starting it is what puts the indicator on screen.
"""
from .session import ControlError, ControlOptions, ControlSession

__all__ = ["ControlError", "ControlOptions", "ControlSession"]
