"""Autenticador WebAuthn de software (ES256, atestación "none") para probar el flujo de passkey."""
import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Autenticador:
    def __init__(self, rp_id: str, origen: str):
        self.rp_id, self.origen = rp_id, origen
        self.clave = ec.generate_private_key(ec.SECP256R1())
        self.id_cred = os.urandom(32)
        self.contador = 0

    def _cliente(self, tipo: str, reto_b64: str) -> bytes:
        return json.dumps({"type": tipo, "challenge": reto_b64, "origin": self.origen,
                           "crossOrigin": False}).encode()

    def _auth_data(self, flags: int, extra: bytes = b"") -> bytes:
        self.contador += 1
        return hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.contador) + extra

    def crear(self, opciones: dict) -> dict:
        nums = self.clave.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        datos_cred = bytes(16) + struct.pack(">H", len(self.id_cred)) + self.id_cred + cose
        auth = self._auth_data(0x45, datos_cred)   # UP | UV | AT
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64(self.id_cred), "rawId": b64(self.id_cred), "type": "public-key",
                "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(self._cliente("webauthn.create", opciones["challenge"])),
                             "attestationObject": b64(att), "transports": ["internal"]}}

    def firmar(self, opciones: dict, uv: bool = True) -> dict:
        cliente = self._cliente("webauthn.get", opciones["challenge"])
        auth = self._auth_data(0x05 if uv else 0x01)
        firma = self.clave.sign(auth + hashlib.sha256(cliente).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.id_cred), "rawId": b64(self.id_cred), "type": "public-key",
                "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(cliente), "authenticatorData": b64(auth),
                             "signature": b64(firma), "userHandle": b64(b"mi-diabetes-usuario-unico")}}
