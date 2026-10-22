"""Passkey ceremonies and signed, revocable owner sessions.

WebAuthn parsing/signature verification belongs to the pinned library. The host
adds one-use browser binding, exact origin, cross-origin rejection and storage.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Callable

from webauthn import (generate_authentication_options, generate_registration_options,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, options_to_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                     ResidentKeyRequirement, UserVerificationRequirement)

from .settings import DashboardSettings
from .store import AuthRejected, AuthStore, digest

SESSION_SECONDS = 43_200  # Proposed: twelve hours, with no sliding extension.


def _matches(expected: str, supplied: str) -> bool:
    return (isinstance(supplied, str) and len(supplied) <= 256
            and hmac.compare_digest(expected.encode(), supplied.encode()))


@dataclass(frozen=True, slots=True)
class VerifiedOwner:
    owner_id: str
    session_id: str = field(repr=False)


class AuthService:
    def __init__(self, settings: DashboardSettings, *, store: AuthStore | None = None,
                 clock: Callable[[], float] = time.time):
        settings.validate()
        if not settings.enabled:
            raise ValueError("Dashboard is not enabled")
        self.settings = settings
        self.clock = clock
        # Read only the credential file. Never return it or include its path/data in errors.
        try:
            if os.name != "nt" and settings.session_secret_file.stat().st_mode & 0o077:
                raise ValueError("Dashboard session credential must be private to its owner")
            with settings.session_secret_file.open("rb") as secret_file:
                secret = secret_file.read(4097)
        except OSError:
            raise ValueError("Dashboard session credential could not be read") from None
        if not 32 <= len(secret) <= 4096:
            raise ValueError("Dashboard session credential must contain at least 32 random bytes")
        self._secret = secret
        self.store = store or AuthStore(settings.state_file, owner=settings.owner_id,
                                       origin=settings.origin, rp_id=settings.rp_id)

    def now(self):
        return int(self.clock())

    def _sign(self, purpose: str, value: str) -> str:
        return hmac.new(self._secret, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    def browser_context(self, existing: str | None) -> tuple[str, str]:
        token = existing if isinstance(existing, str) and len(existing) <= 128 else None
        if token is None or not self.store.browser(token, self.now()):
            token = secrets.token_urlsafe(32)
            self.store.browser(token, self.now(), create=True)
        return token, self._sign("browser-csrf", token)

    def verify_browser(self, token: str, csrf: str):
        if (not token or len(token) > 128 or not csrf
                or not _matches(self._sign("browser-csrf", token), csrf)
                or not self.store.browser(token, self.now())):
            raise AuthRejected()

    def issue_enrollment(self) -> str:
        token = secrets.token_urlsafe(32)
        self.store.issue_enrollment(token, self.now())
        self.store.audit(self.now(), "enrollment_issued")
        return token

    def options(self, kind: str, browser: str, *, token: str | None = None) -> dict:
        challenge = secrets.token_bytes(32)
        token_hash = None
        if kind == "enrol":
            if not isinstance(token, str) or not 40 <= len(token) <= 128:
                raise AuthRejected()
            token_hash = digest(token)
            if not self.store.enrollment_valid(token_hash, self.now()):
                raise AuthRejected()
            options = generate_registration_options(
                rp_id=self.settings.rp_id, rp_name="My day", user_name="Owner",
                user_id=self.store.user_handle(), challenge=challenge,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED, require_resident_key=True,
                    user_verification=UserVerificationRequirement.REQUIRED),
                exclude_credentials=[PublicKeyCredentialDescriptor(id=value)
                                     for value in self.store.credential_ids()],
            )
        elif kind == "sign-in":
            options = generate_authentication_options(rp_id=self.settings.rp_id, challenge=challenge,
                                                       user_verification=UserVerificationRequirement.REQUIRED)
        else:
            raise ValueError("Unknown dashboard ceremony")
        challenge_id = self.store.add_challenge(browser, kind, challenge, self.now(), token_hash)
        return {"challenge_id": challenge_id, "publicKey": json.loads(options_to_json(options))}

    def verify(self, kind: str, browser: str, body: dict) -> str:
        event = "enrollment" if kind == "enrol" else "sign_in"
        try:
            challenge_id = body["challenge_id"]
            if not isinstance(challenge_id, str) or len(challenge_id) > 128:
                raise AuthRejected()
            challenge = self.store.consume_challenge(challenge_id, browser, kind, self.now())
            credential = body["credential"]
            client = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
            # py_webauthn verifies origin but intentionally leaves the embedding
            # policy to its caller. This application never accepts embedded ceremonies.
            if not isinstance(client, dict) or client.get("crossOrigin", False) is not False or "topOrigin" in client:
                raise AuthRejected()
            kwargs = dict(credential=credential, expected_challenge=challenge["challenge"],
                          expected_rp_id=self.settings.rp_id, expected_origin=self.settings.origin,
                          require_user_verification=True)
            if kind == "enrol":
                verified = verify_registration_response(**kwargs)
                self.store.register(challenge["enrollment_hash"], verified, self.now())
            elif kind == "sign-in":
                credential_id = base64url_to_bytes(credential["id"])
                stored = self.store.credential(credential_id)
                if stored is None:
                    raise AuthRejected()
                # Discoverable sign-in must identify this sole owner as well as
                # present a known public key; request text cannot choose an owner.
                user = credential["response"].get("userHandle")
                if not isinstance(user, str) or base64url_to_bytes(user) != self.store.user_handle():
                    raise AuthRejected()
                verified = verify_authentication_response(
                    **kwargs, credential_public_key=stored["public_key"],
                    credential_current_sign_count=stored["sign_count"])
                self.store.update_counter(verified.credential_id, stored["sign_count"], verified.new_sign_count)
            else:
                raise AuthRejected()
        except (AuthRejected, WebAuthnException, ValueError, TypeError, KeyError, AttributeError):
            self.store.audit(self.now(), f"{event}_failed")
            raise AuthRejected() from None
        session = self._new_session(verified.credential_id)
        self.store.audit(self.now(), f"{event}_ok")
        return session

    def _new_session(self, credential_id: bytes) -> str:
        token = secrets.token_urlsafe(32)
        expires = self.now() + SESSION_SECONDS
        self.store.new_session(token, credential_id, expires)
        payload = f"{token}.{expires}"
        return f"{payload}.{self._sign('session', payload)}"

    def owner(self, cookie: str | None) -> VerifiedOwner:
        if not isinstance(cookie, str) or len(cookie) > 256:
            raise AuthRejected()
        try:
            token, expiry, signature = cookie.split(".")
            expires = int(expiry)
        except (ValueError, TypeError):
            raise AuthRejected() from None
        if (not _matches(self._sign("session", f"{token}.{expiry}"), signature)
                or expires <= self.now() or not self.store.valid_session(token, expires, self.now())):
            raise AuthRejected()
        return VerifiedOwner(self.settings.owner_id, token)

    def session_csrf(self, owner: VerifiedOwner) -> str:
        return self._sign("session-csrf", owner.session_id)

    def verify_session_csrf(self, owner: VerifiedOwner, csrf: str):
        if not csrf or not _matches(self.session_csrf(owner), csrf):
            raise AuthRejected()

    def logout(self, owner: VerifiedOwner):
        self.store.logout(owner.session_id)
        self.store.audit(self.now(), "signed_out")

    def revoke_all(self):
        self.store.revoke_all()
        self.store.audit(self.now(), "revoked_all")
