"""Temporal workflow worker composition root process shell.

The process shell registers four durable worker loops: the projection
dispatcher, the policy preview worker, the policy reconciliation worker and
the multipart exact-cleanup worker (``run-multipart-cleanup``): the
registered Temporal cleanup workflow/activity plus the bounded sweep
dispatcher of spec 6.4.
"""
