"""Connection intent and recovery state for the MSFS window."""


class FsdRecovery:
    """Track whether an unexpected simulator outage needs an FSD restart."""

    def __init__(self):
        self._connect_requested = False
        self._sim_was_lost = False
        self._recovery_pending = False

    def connect_requested(self):
        self._connect_requested = True
        self._sim_was_lost = False
        self._recovery_pending = False

    def manual_disconnect(self):
        self._connect_requested = False
        self._sim_was_lost = False
        self._recovery_pending = False

    def on_sim_state(self, connected, *, observer):
        if observer or not self._connect_requested:
            return False
        if not connected:
            self._sim_was_lost = True
            return False
        if not self._sim_was_lost:
            return False
        self._sim_was_lost = False
        self._recovery_pending = True
        return True

    def consume_recovery(self):
        if not self._recovery_pending:
            return False
        self._recovery_pending = False
        return True
