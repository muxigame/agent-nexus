"""API providers as agents, client-direct (docs/agent-chat.md §16.4).

The runner device talks to the model provider itself (never through the gateway): :mod:`.protocols` speaks the
three streaming wire formats, :mod:`.loop` is the agent loop driven by the runner (an in-process adapter, no
subprocess), :mod:`.tools` are the local tools it may call, :mod:`.keys` keeps the keys on this device only,
:mod:`.store` the sessions, :mod:`.config` the provider list (local ``agents.api.providers`` merged with the
gateway's synced copy) and :mod:`.models` the built-in context windows.
"""
