from core.adapters.pure_and_cis import PureClient, TaborClient
from core.adapters_registry import GLOBAL_DATING_REGISTRY
from core.base_client import BaseDatingClient
from core.client import TwinbyStealthClient
from core.proxy.manager import GeoProxyManager, Region

IMPLEMENTED_CLIENTS = frozenset({"twinby", "pure", "tabor"})


def create_dating_client(
    platform: str,
    session_id: str,
    credentials: dict[str, str],
    proxy_manager: GeoProxyManager,
) -> BaseDatingClient:
    plat = platform.lower()
    spec = GLOBAL_DATING_REGISTRY.get(plat)
    if not spec:
        raise ValueError(
            f"Платформа '{platform}' отсутствует в GLOBAL_DATING_REGISTRY."
        )
    if plat not in IMPLEMENTED_CLIENTS:
        raise NotImplementedError(f"No verified adapter contract for {plat}")
    token = credentials.get("token", "").strip()
    if not token:
        raise ValueError("An explicit account token is required")
    region_map = {
        "CIS": Region.CIS,
        "US": Region.US,
        "EU": Region.EU,
        "ASIA": Region.ASIA,
    }
    proxy_url = proxy_manager.get_proxy_for_session(
        session_id, region=region_map.get(spec.region, Region.ANY)
    )
    if plat == "twinby":
        client = TwinbyStealthClient(
            access_token=token,
            refresh_token=credentials.get("refresh_token"),
            proxy_url=proxy_url,
        )
        client.session_id = session_id
        return client
    if plat == "pure":
        return PureClient(device_token=token, proxy_url=proxy_url)
    return TaborClient(session_token=token, proxy_url=proxy_url)
