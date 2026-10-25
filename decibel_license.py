# COPIED from DecibelLicense (c7ada7f 2026-10-24) by Scripts/vendor-into.sh — do not edit here.
"""Reads a Decibel One license file (FORMAT.md) with nothing but the standard library.

SiRPS carries this file. The Ed25519 check is RFC 8032's own reference algorithm, so the
app gains no dependency; it is slow next to a C library (tens of milliseconds) and that is
fine for one check at launch. `sign` exists for the tools and proofs, never for an app.
"""
import base64
import binascii
import hashlib
import json
import re

# --- Ed25519 (RFC 8032, section 6) -------------------------------------------------------

_p = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _sha512_mod_l(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _L


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * pow(5, _p - 2, _p) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P):
    zinv = pow(P[2], _p - 2, _p)
    x, y = P[0] * zinv % _p, P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _expand(secret):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret):
    """The 32-byte public key for a 32-byte secret (tools only)."""
    a, _ = _expand(secret)
    return _compress(_mul(a, _G))


def sign(secret, message):
    """A 64-byte Ed25519 signature (tools and proofs only)."""
    a, prefix = _expand(secret)
    A = _compress(_mul(a, _G))
    r = _sha512_mod_l(prefix + message)
    R = _compress(_mul(r, _G))
    s = (r + _sha512_mod_l(R + A + message) * a) % _L
    return R + int.to_bytes(s, 32, "little")


def verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    R = _decompress(signature[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = _sha512_mod_l(signature[:32] + public + message)
    return _equal(_mul(s, _G), _add(R, _mul(h, A)))


# --- The license ---------------------------------------------------------------------------

# Keys a reader accepts: id -> (public key, is a test key). The production key is added
# when it is made on the droplet; test keys pass only when allow_test_keys is set.
KEYS = {
    "test-2026": (base64.b64decode("voifu24gC/eG5bwRgbBswRAJjExn8TPL6L/kEi1WsBA="), True),
}

PERSONAL_LINE = "Student, teacher and non-pro use"


class LicenseError(Exception):
    """`.reason` is one of: malformed, unknown_key, test_key_not_allowed, bad_signature,
    unsupported_format."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _b64(text):
    if not isinstance(text, str):
        raise LicenseError("malformed")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise LicenseError("malformed")


def read(data, allow_test_keys=False, keys=None):
    """Parses and checks a license file's bytes. Returns the payload as a dict; raises
    LicenseError."""
    keys = KEYS if keys is None else keys
    try:
        outer = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except (UnicodeDecodeError, ValueError):
        raise LicenseError("malformed")
    if not isinstance(outer, dict) or outer.get("type") != "decibel-one-license":
        raise LicenseError("malformed")
    key_id = outer.get("key")
    if not isinstance(key_id, str):
        raise LicenseError("malformed")
    if key_id not in keys:
        raise LicenseError("unknown_key")
    public, is_test = keys[key_id]
    if is_test and not allow_test_keys:
        raise LicenseError("test_key_not_allowed")
    payload, signature = _b64(outer.get("payload")), _b64(outer.get("signature"))
    if len(signature) != 64:
        raise LicenseError("malformed")
    if not verify(public, payload, signature):
        raise LicenseError("bad_signature")
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LicenseError("malformed")
    if not isinstance(body, dict):
        raise LicenseError("malformed")
    if "format" not in body:
        raise LicenseError("malformed")
    if _whole(body["format"]) != 1:
        raise LicenseError("unsupported_format")
    if not all(isinstance(body.get(k), str) for k in ("id", "licensee", "issued")):
        raise LicenseError("malformed")
    apps = body.get("apps")
    if not isinstance(apps, list):
        raise LicenseError("malformed")
    for entry in apps:
        if not isinstance(entry, dict) or not isinstance(entry.get("app"), str):
            raise LicenseError("malformed")
        if _whole(entry.get("major")) is None or _whole(entry["major"]) < 0:
            raise LicenseError("malformed")
        entry["major"] = _whole(entry["major"])
        if entry.get("tier") not in ("pro", "company"):
            raise LicenseError("malformed")
        if entry["tier"] == "company":
            if _whole(entry.get("macs")) is None or _whole(entry["macs"]) < 1:
                raise LicenseError("malformed")
            entry["macs"] = _whole(entry["macs"])
    return body


def _whole(v):
    """A JSON whole number as an int, or None. Never a boolean or a fraction; 2.0 counts as
    2, because Qt's JSON parser cannot tell the two apart and the readers must agree."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer() and abs(v) < 2**31:
        return int(v)
    return None


def major_of(version):
    """The leading number of a version string: "1.28.1" -> 1, "v0.6.57" -> 0."""
    m = re.match(r"\s*[vV]?(\d+)", version or "")
    return int(m.group(1)) if m else None


def covers(license, app, version):
    """The entry that covers this app at this version, or None."""
    if license is None:
        return None
    major = major_of(version)
    if major is None:
        return None
    for entry in license["apps"]:
        if entry["app"] == app and (entry["major"] == major or (entry["major"] == 0 and major == 1)):
            return entry
    return None


def line(license, app, version):
    """What the About box and exports say."""
    entry = covers(license, app, version)
    if entry is None:
        return PERSONAL_LINE
    text = f"Licensed to {license['licensee']}"
    if entry["tier"] == "company":
        text += f" · {entry['macs']} Mac" + ("" if entry["macs"] == 1 else "s")
    return text
