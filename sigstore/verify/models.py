# Copyright 2022 The Sigstore Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Common (base) models for the verification APIs.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from textwrap import dedent
from typing import IO

from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import (
    Certificate,
    load_der_x509_certificate,
    load_pem_x509_certificate,
)
from pydantic import BaseModel
from sigstore_protobuf_specs.dev.sigstore.bundle.v1 import Bundle

from sigstore._internal.rekor import RekorClient
from sigstore._utils import (
    B64Str,
    PEMCert,
    base64_encode_pem_cert,
    cert_is_leaf,
    cert_is_root_ca,
    sha256_streaming,
)
from sigstore.errors import Error
from sigstore.transparency import LogEntry, LogInclusionProof

logger = logging.getLogger(__name__)


class VerificationResult(BaseModel):
    """
    Represents the result of a verification operation.

    Results are boolish, and failures contain a reason (and potentially
    some additional context).
    """

    success: bool
    """
    Represents the status of this result.
    """

    def __bool__(self) -> bool:
        """
        Returns a boolean representation of this result.

        `VerificationSuccess` is always `True`, and `VerificationFailure`
        is always `False`.
        """
        return self.success


class VerificationSuccess(VerificationResult):
    """
    The verification completed successfully,
    """

    success: bool = True
    """
    See `VerificationResult.success`.
    """


class VerificationFailure(VerificationResult):
    """
    The verification failed, due to `reason`.
    """

    success: bool = False
    """
    See `VerificationResult.success`.
    """

    reason: str
    """
    A human-readable explanation or description of the verification failure.
    """


class InvalidMaterials(Error):
    """
    Raised when the associated `VerificationMaterials` are invalid in some way.
    """

    def diagnostics(self) -> str:
        """Returns diagnostics for the error."""

        return dedent(
            f"""\
        An issue occurred while parsing the verification materials.

        The provided verification materials are malformed and may have been
        modified maliciously.

        Additional context:

        {self}
        """
        )


class RekorEntryMissing(Exception):
    """
    Raised if `VerificationMaterials.rekor_entry()` fails to find an entry
    in the Rekor log.

    This is an internal exception; users should not see it.
    """

    pass


class InvalidRekorEntry(InvalidMaterials):
    """
    Raised if the effective Rekor entry in `VerificationMaterials.rekor_entry()`
    does not match the other materials in `VerificationMaterials`.

    This can only happen in two scenarios:

    * A user has supplied the wrong offline entry, potentially maliciously;
    * The Rekor log responded with the wrong entry, suggesting a server error.
    """

    pass


@dataclass(init=False)
class VerificationMaterials:
    """
    Represents the materials needed to perform a Sigstore verification.
    """

    input_digest: bytes
    """
    The SHA256 hash of the verification input, as raw bytes.
    """

    certificate: Certificate
    """
    The certificate that attests to and contains the public signing key.

    # TODO: Support a certificate chain here, with optional intermediates.
    """

    signature: bytes
    """
    The raw signature.
    """

    _offline: bool
    """
    Whether to do offline Rekor entry verification.

    NOTE: This is intentionally not a public field, since it's slightly
    mismatched against the other members of `VerificationMaterials` -- it's
    more of an option than a piece of verification material.
    """

    _rekor_entry: LogEntry | None
    """
    An optional Rekor entry.

    If a Rekor entry is supplied **and** `offline` is set to `True`,
    verification will be done against this entry rather than the against the
    online transparency log. If not provided **or** `offline` is `False` (the
    default), then the online transparency log will be used.

    NOTE: This is **intentionally not a public field**. The `rekor_entry()`
    method should be used to access a Rekor log entry for these materials,
    as it performs the online lookup if an offline entry is not provided
    and, **critically**, validates that the entry's contents match the other
    signing materials. Without this check an adversary could present a
    **valid but unrelated** Rekor entry during verification, similar
    to CVE-2022-36056 in cosign.

    TODO: Support multiple entries here, with verification contingent on
    all being valid.
    """

    def __init__(
        self,
        *,
        input_: IO[bytes],
        cert_pem: PEMCert,
        signature: bytes,
        offline: bool = False,
        rekor_entry: LogEntry | None,
    ):
        """
        Create a new `VerificationMaterials` from the given materials.

        `offline` controls the behavior of any subsequent verification over
        these materials: if `True`, the supplied Rekor entry (which must
        be supplied) will be verified via its Signed Entry Timestamp, but
        its proof of inclusion will not be checked. This is a slightly weaker
        verification mode, as it demonstrates that an entry has been signed by
        the log but not necessarily included in it.

        Effect: `input_` is consumed as part of construction.
        """

        self.input_digest = sha256_streaming(input_)
        self.certificate = load_pem_x509_certificate(cert_pem.encode())
        self.signature = signature

        # Invariant: requesting offline verification means that a Rekor entry
        # *must* be provided.
        if offline and not rekor_entry:
            raise InvalidMaterials("offline verification requires a Rekor entry")

        self._offline = offline
        self._rekor_entry = rekor_entry

    @classmethod
    def from_bundle(
        cls, *, input_: IO[bytes], bundle: Bundle, offline: bool = False
    ) -> VerificationMaterials:
        """
        Create a new `VerificationMaterials` from the given Sigstore bundle.

        Effect: `input_` is consumed as part of construction.
        """
        certs = bundle.verification_material.x509_certificate_chain.certificates

        if len(certs) == 0:
            raise InvalidMaterials("expected non-empty certificate chain in bundle")

        # Per client policy in protobuf-specs: the first entry in the chain
        # MUST be a leaf certificate, and the rest of the chain MUST NOT
        # include a root CA or any intermediate CAs that appear in an
        # independent root of trust.
        #
        # We expect some old bundles to violate the rules around root
        # and intermediate CAs, so we issue warnings and not hard errors
        # in those cases.
        leaf_cert, *chain_certs = [
            load_der_x509_certificate(cert.raw_bytes) for cert in certs
        ]
        if not cert_is_leaf(leaf_cert):
            raise InvalidMaterials(
                "bundle contains an invalid leaf or non-leaf certificate in the leaf position"
            )

        for chain_cert in chain_certs:
            # TODO: We should also retrieve the root of trust here and
            # cross-check against it.
            if cert_is_root_ca(chain_cert):
                logger.warning(
                    "this bundle contains a root CA, making it subject to misuse"
                )

        signature = bundle.message_signature.signature

        tlog_entries = bundle.verification_material.tlog_entries
        if len(tlog_entries) != 1:
            raise InvalidMaterials(
                f"expected exactly one log entry, got {len(tlog_entries)}"
            )
        tlog_entry = tlog_entries[0]

        # NOTE: Bundles are not required to include inclusion proofs,
        # since offline (or non-gossiped) verification of an inclusion proof is
        # only as strong as verification of the inclusion promise, which
        # is always provided.
        inclusion_proof = tlog_entry.inclusion_proof
        parsed_inclusion_proof: LogInclusionProof | None = None
        if inclusion_proof:
            checkpoint = inclusion_proof.checkpoint

            # If the inclusion proof is provided, it must include its
            # checkpoint.
            if not checkpoint.envelope:
                raise InvalidMaterials("expected checkpoint in inclusion proof")

            parsed_inclusion_proof = LogInclusionProof(
                checkpoint=checkpoint.envelope,
                hashes=[h.hex() for h in inclusion_proof.hashes],
                log_index=inclusion_proof.log_index,
                root_hash=inclusion_proof.root_hash.hex(),
                tree_size=inclusion_proof.tree_size,
            )

        entry = LogEntry(
            uuid=None,
            body=B64Str(base64.b64encode(tlog_entry.canonicalized_body).decode()),
            integrated_time=tlog_entry.integrated_time,
            log_id=tlog_entry.log_id.key_id.hex(),
            log_index=tlog_entry.log_index,
            inclusion_proof=parsed_inclusion_proof,
            inclusion_promise=B64Str(
                base64.b64encode(
                    tlog_entry.inclusion_promise.signed_entry_timestamp
                ).decode()
            ),
        )

        return cls(
            input_=input_,
            cert_pem=PEMCert(leaf_cert.public_bytes(Encoding.PEM).decode()),
            signature=signature,
            offline=offline,
            rekor_entry=entry,
        )

    @property
    def has_rekor_entry(self) -> bool:
        """
        Returns whether or not these `VerificationMaterials` contain a Rekor
        entry.

        If false, `VerificationMaterials.rekor_entry()` performs an online lookup.
        """
        return self._rekor_entry is not None

    def rekor_entry(self, client: RekorClient) -> LogEntry:
        """
        Returns a `RekorEntry` for the current signing materials.
        """

        # The Rekor entry we use depends on a few different states:
        # 1. If the user has requested offline verification and we've
        #    been given an offline Rekor entry to use, we use it.
        # 2. If the user has not requested offline verification,
        #    we *opportunistically* use the offline Rekor entry,
        #    so long as it contains an inclusion proof. If it doesn't
        #    contain an inclusion proof, then we do an online entry lookup.
        offline = self._offline
        has_rekor_entry = self.has_rekor_entry
        has_inclusion_proof = (
            self.has_rekor_entry and self._rekor_entry.inclusion_proof is not None  # type: ignore
        )

        entry: LogEntry | None
        if (offline and has_rekor_entry) or (not offline and has_inclusion_proof):
            logger.debug("using offline rekor entry")
            entry = self._rekor_entry
        else:
            logger.debug("retrieving rekor entry")
            entry = client.log.entries.retrieve.post(
                self.signature,
                self.input_digest.hex(),
                self.certificate,
            )

        # No matter what we do above, we must end up with a Rekor entry.
        if entry is None:
            raise RekorEntryMissing

        # To verify that an entry matches our other signing materials,
        # we transform our signature, artifact hash, and certificate
        # into a "hashedrekord" style payload and compare it against the
        # entry's own body.
        #
        # This is done by:
        #
        # * Serializing the certificate as PEM, and then base64-encoding it;
        # * base64-encoding the signature;
        # * Packing the resulting cert, signature, and hash into the
        #   hashedrekord body format;
        # * Comparing that body against the entry's own body, which
        #   is extracted from its base64(json(...)) encoding.

        logger.debug("Rekor entry: ensuring contents match signing materials")

        expected_body = {
            "kind": "hashedrekord",
            "apiVersion": "0.0.1",
            "spec": {
                "signature": {
                    "content": B64Str(base64.b64encode(self.signature).decode()),
                    "publicKey": {
                        "content": B64Str(base64_encode_pem_cert(self.certificate))
                    },
                },
                "data": {
                    "hash": {"algorithm": "sha256", "value": self.input_digest.hex()}
                },
            },
        }

        actual_body = json.loads(base64.b64decode(entry.body))

        if expected_body != actual_body:
            raise InvalidRekorEntry

        return entry
