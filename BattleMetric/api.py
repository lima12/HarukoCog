import asyncio
import logging
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urljoin

import aiohttp


log = logging.getLogger("red.BattleMetric.api")


class BattleMetricsAPIError(Exception):
    """Raised when BattleMetrics returns an error response."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        response: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(message)
        self.status = status
        self.response = response


class BattleMetricsClient:
    """Small async client for BattleMetrics JSON:API endpoints."""

    BASE_URL = "https://api.battlemetrics.com"

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        timeout: int = 20,
        user_agent: str = "Red-BattleMetric-Cog/1.0",
    ):
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._user_agent = user_agent
        self._session: Optional[aiohttp.ClientSession] = None

    def set_token(self, token: Optional[str]) -> None:
        self._token = token.strip() if token else None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Mapping[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        """Make a BattleMetrics request and return the decoded JSON document.

        `params` may use either raw JSON:API keys such as `filter[search]` or
        nested dictionaries such as `{"filter": {"search": "rust"}}`.
        """
        url = self._build_url(path)
        headers = self._build_headers(auth=auth)
        flat_params = self._flatten_params(params or {})
        session = await self._get_session()

        try:
            async with session.request(
                method.upper(),
                url,
                params=flat_params,
                json=json,
                headers=headers,
            ) as response:
                return await self._decode_response(response)
        except asyncio.TimeoutError as exc:
            raise BattleMetricsAPIError("BattleMetrics request timed out.") from exc
        except aiohttp.ClientError as exc:
            raise BattleMetricsAPIError(f"BattleMetrics request failed: {exc}") from exc

    async def get(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        return await self.request("GET", path, params=params, auth=auth)

    async def get_server(
        self,
        server_id: str,
        *,
        include: Optional[str] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        params = {"include": include} if include else None
        return await self.get(f"/servers/{server_id}", params=params, auth=auth)

    async def list_servers(
        self,
        *,
        search: Optional[str] = None,
        game: Optional[str] = None,
        page_size: int = 5,
        auth: bool = True,
    ) -> Dict[str, Any]:
        page_size = max(1, min(int(page_size), 100))
        params: Dict[str, Any] = {"page": {"size": page_size}}

        filters: Dict[str, str] = {}
        if search:
            filters["search"] = search
        if game:
            filters["game"] = game
        if filters:
            params["filter"] = filters

        return await self.get("/servers", params=params, auth=auth)

    async def get_player(
        self,
        player_id: str,
        *,
        include: Optional[str] = None,
        server_id: Optional[str] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if include:
            params["include"] = include
        if server_id:
            params["filter"] = {"servers": server_id}
        return await self.get(
            f"/players/{player_id}",
            params=params or None,
            auth=auth,
        )

    async def quick_match_player_identifiers(
        self,
        identifier: str,
        identifier_types: tuple[str, ...],
    ) -> Dict[str, Any]:
        """Match a game identifier to its BattleMetrics player resource."""
        data = [
            {
                "type": "identifier",
                "attributes": {
                    "type": identifier_type,
                    "identifier": identifier,
                },
            }
            for identifier_type in identifier_types
        ]
        return await self.request(
            "POST",
            "/players/quick-match",
            json={"data": data},
            auth=True,
        )

    async def get_player_server_information(
        self,
        player_id: str,
        server_id: str,
        *,
        auth: bool = True,
    ) -> Dict[str, Any]:
        """Return one player's cumulative information for a server."""
        return await self.get(
            f"/players/{player_id}/servers/{server_id}",
            auth=auth,
        )

    async def list_players(
        self,
        *,
        search: Optional[str] = None,
        page_size: int = 5,
        auth: bool = True,
    ) -> Dict[str, Any]:
        page_size = max(1, min(int(page_size), 100))
        params: Dict[str, Any] = {"page": {"size": page_size}}
        if search:
            params["filter"] = {"search": search}
        return await self.get("/players", params=params, auth=auth)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _build_headers(self, *, auth: bool) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _build_url(self, path: str) -> str:
        path = str(path).strip()
        if path.startswith(("http://", "https://")):
            return path
        return urljoin(f"{self.BASE_URL}/", path.lstrip("/"))

    async def _decode_response(self, response: aiohttp.ClientResponse) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}

        if response.content_length != 0:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                payload = await response.json()
            else:
                text = await response.text()
                payload = {"errors": [{"detail": text[:500]}]}

        if 200 <= response.status < 300:
            return payload

        raise BattleMetricsAPIError(
            self._error_message(response.status, payload),
            status=response.status,
            response=payload,
        )

    @classmethod
    def _flatten_params(cls, params: Mapping[str, Any]) -> Dict[str, str]:
        flattened: Dict[str, str] = {}

        def add(prefix: str, value: Any) -> None:
            if value is None:
                return
            if isinstance(value, Mapping):
                for key, nested_value in value.items():
                    add(f"{prefix}[{key}]", nested_value)
                return
            if isinstance(value, (list, tuple, set)):
                flattened[prefix] = ",".join(str(item) for item in value if item is not None)
                return
            flattened[prefix] = str(value)

        for key, value in params.items():
            if isinstance(value, Mapping) and "[" not in str(key):
                add(str(key), value)
            else:
                add(str(key), value)

        return flattened

    @staticmethod
    def _error_message(status: int, payload: Mapping[str, Any]) -> str:
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, Mapping):
                detail = first.get("detail") or first.get("title")
                if detail:
                    return f"BattleMetrics API error {status}: {detail}"
        return f"BattleMetrics API error {status}."
