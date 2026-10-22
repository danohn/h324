"""Shared cooperative teardown budgets, in seconds.

The outer process fallback remains necessary for an unresponsive OS driver.
The cleanup deadline includes transmitter join, EOT and all restoration steps;
a serial write already in progress can finish up to its bounded timeout later.
"""
SESSION_CLOSE_TIMEOUT = 10.0
CLEANUP_BUDGET = 12.0
SERIAL_WRITE_TIMEOUT = 3.0
API_HANGUP_TIMEOUT = SESSION_CLOSE_TIMEOUT+CLEANUP_BUDGET+SERIAL_WRITE_TIMEOUT+2.0
WORKER_SHUTDOWN_TIMEOUT = API_HANGUP_TIMEOUT+3.0
GUI_WORKER_SHUTDOWN_TIMEOUT = WORKER_SHUTDOWN_TIMEOUT+2.0
