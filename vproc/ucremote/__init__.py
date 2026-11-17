"""Unfolded Circle Remote integration driver for vproc.

Runs as its own process (``python -m vproc.ucremote``), started and stopped by
vproc when the integration is switched on or off in the web UI. It exposes
vproc to the remote as a handful of entities and talks to vproc only through
vproc's own HTTP API, exactly like the web page does.
"""
