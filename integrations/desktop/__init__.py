"""Desktop remote-workbench integrations (phase 2+).

Change ``add-desktop-remote-web-workbench``. This package is the server half of
the device / directory / command / transfer surface: authorization (``access``),
device and workspace records (``devices``), durable commands (``commands``),
chunked uploads (``transfers``), and the gateway. Nothing here is a second
identity or authorization truth -- every check reuses ``IdentityService`` and
the existing Membership / Agent / session seams (contracts §9).
"""
