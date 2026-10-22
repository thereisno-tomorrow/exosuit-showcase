"""Source-tree bridge to the hub client that ships beside ``provider.py``.

``install-manifest.json`` in this folder maps ``adapters/hermes/hub_client.py``
straight to ``/opt/data/plugins/brain/hub_client.py``. This bridge lets repository
imports use that one implementation without a second copy in source. It is never
installed.
"""

from adapters.hermes.hub_client import HubClientConfig, HubClientError, HubHTTPSClient

__all__ = ["HubClientConfig", "HubClientError", "HubHTTPSClient"]
