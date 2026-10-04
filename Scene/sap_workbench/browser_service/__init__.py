"""G1 browser execution node for the SAP workbench.

Split deliberately: :mod:`mapping` and :mod:`tokens` are pure and unit-tested,
:mod:`gateway` owns the loopback WebSocket the pane talks to, :mod:`node` talks
CDP to a real Chrome, and :mod:`runner` hosts the gateway beside the WSGI app.
"""
