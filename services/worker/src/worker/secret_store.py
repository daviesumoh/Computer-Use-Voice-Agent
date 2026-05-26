"""Where website credentials live. The database holds only an opaque reference."""

import asyncio
from typing import Protocol
from uuid import UUID

from bitwarden_sdk import BitwardenClient, ClientSettings, DeviceType


class SecretStoreError(Exception):
    pass


class SecretStore(Protocol):
    async def create(self, name: str, value: str) -> str:
        """Stores a secret and returns its reference."""
        ...

    async def read(self, ref: str) -> str: ...

    async def delete(self, ref: str) -> None: ...


class BitwardenSecretStore:
    """Bitwarden Secrets Manager. The SDK is synchronous, so calls run in a worker thread."""

    def __init__(
        self, *, access_token: str, organization_id: UUID, project_id: UUID, state_file: str
    ) -> None:
        self._organization_id = organization_id
        self._project_id = project_id
        self._client = BitwardenClient(
            ClientSettings(
                api_url="https://api.bitwarden.com",
                identity_url="https://identity.bitwarden.com",
                device_type=DeviceType.SDK,
                user_agent="agent-worker",
            )
        )
        login = self._client.auth().login_access_token(access_token, state_file)
        if not login.success:
            raise SecretStoreError(f"Bitwarden login failed: {login.error_message}")

    async def create(self, name: str, value: str) -> str:
        response = await asyncio.to_thread(
            self._client.secrets().create,
            self._organization_id,
            name,
            value,
            "",
            [self._project_id],
        )
        if not response.success or response.data is None:
            raise SecretStoreError(f"Bitwarden create failed: {response.error_message}")
        return str(response.data.id)

    async def read(self, ref: str) -> str:
        response = await asyncio.to_thread(self._client.secrets().get, ref)
        if not response.success or response.data is None:
            raise SecretStoreError(f"Bitwarden read failed: {response.error_message}")
        return str(response.data.value)

    async def delete(self, ref: str) -> None:
        response = await asyncio.to_thread(self._client.secrets().delete, [ref])
        if not response.success:
            raise SecretStoreError(f"Bitwarden delete failed: {response.error_message}")
