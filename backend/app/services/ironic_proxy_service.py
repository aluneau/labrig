"""Host-owned HTTP image proxy for physical BMC virtual media."""
import json
from app.services.helper_service import run_helper


class IronicProxyService:
    def status(self):
        return json.loads(run_helper(["ironic-proxy", "status"]))

    def configure(self, listen_ip, backend_ip, sources):
        run_helper(["ironic-proxy", "configure", listen_ip, backend_ip, ",".join(sources)], timeout=60)
        # The helper's configure output is the requested configuration; ask status again so the
        # UI receives the derived `configured` field after HAProxy has accepted the generated file.
        return self.status()

    def disable(self):
        return json.loads(run_helper(["ironic-proxy", "disable"], timeout=60))


ironic_proxy_service = IronicProxyService()
