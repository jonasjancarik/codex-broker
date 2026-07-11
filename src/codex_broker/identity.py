from __future__ import annotations

from dataclasses import dataclass

from .config import BrokerConfig
from .util import owner_digest


class AuthPrincipalPolicyError(PermissionError):
    """The requested credential identity is not allowed by trusted-host policy."""


@dataclass(frozen=True)
class AuthScope:
    owner_hash: str
    auth_principal_hash: str
    shared_auth_principal: bool

    def public(self) -> dict[str, str | bool]:
        return {
            "ownerHash": self.owner_hash,
            "authPrincipalHash": self.auth_principal_hash,
            "sharedAuthPrincipal": self.shared_auth_principal,
        }


class AuthPrincipalPolicy:
    """Resolves owner-to-credential mappings configured by the trusted host."""

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self._mappings = dict(config.auth_principal_mappings)

    def resolve(self, owner_id: str, requested_auth_principal_id: str | None = None) -> AuthScope:
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("ownerId must be a non-empty string.")
        configured_principal_id = self._mappings.get(owner_id, owner_id)
        if requested_auth_principal_id is not None:
            if not isinstance(requested_auth_principal_id, str) or not requested_auth_principal_id:
                raise ValueError("authPrincipalId must be a non-empty string.")
            if requested_auth_principal_id != configured_principal_id:
                raise AuthPrincipalPolicyError("authPrincipalId is not permitted for this owner.")
        if configured_principal_id != owner_id and not self.config.internal_key:
            raise AuthPrincipalPolicyError(
                "Shared auth principals require an authenticated trusted-host connection."
            )
        owner_hash = owner_digest(owner_id, self.config.owner_hash_secret)
        principal_hash = owner_digest(configured_principal_id, self.config.owner_hash_secret)
        return AuthScope(
            owner_hash=owner_hash,
            auth_principal_hash=principal_hash,
            shared_auth_principal=self._owner_count(configured_principal_id) > 1,
        )

    def _owner_count(self, principal_id: str) -> int:
        owners = {owner_id for owner_id, target in self._mappings.items() if target == principal_id}
        if self._mappings.get(principal_id, principal_id) == principal_id:
            owners.add(principal_id)
        return len(owners)
