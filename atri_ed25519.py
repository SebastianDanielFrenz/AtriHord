"""Ed25519 (RFC 8032) in pure Python, for checking the signature of the online module index (module_manager.py)
and for making it (tools/publish_modules.py).

Pure Python on purpose: no new dependency for every AtriHord install (the server venv has no `cryptography`), and
one verification per index fetch takes a few milliseconds. This follows the reference algorithm of RFC 8032
section 6 and is checked against the RFC's test vectors (tests/server_checks/module_manager_check.py).
It is not constant-time: fine for VERIFYING public data and for signing on the maintainer's own machine, not for a
server that signs with a secret key on request.
"""
import hashlib

p = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, -1, p) % p
SQRT_M1 = pow(2, (p - 1) // 4, p)


def _recover_x(y, sign):
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(d * y * y + 1, -1, p) % p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p:
        x = x * SQRT_M1 % p
    if (x * x - x2) % p:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


_Gy = 4 * pow(5, -1, p) % p
_Gx = _recover_x(_Gy, 0)
G = (_Gx, _Gy, 1, _Gx * _Gy % p)
_ZERO = (0, 1, 1, 0)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, H, K = B - A, D - C, D + C, B + A
    return (E * F % p, H * K % p, F * H % p, E * K % p)


def _mul(s, P):
    Q = _ZERO
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0


def _compress(P):
    zinv = pow(P[2], -1, p)
    x, y = P[0] * zinv % p, P[1] * zinv % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % p)


def _hash_modl(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % L


def _expand(secret):
    if len(secret) != 32:
        raise ValueError("an Ed25519 secret key is 32 bytes")
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret):
    """The 32-byte public key of a 32-byte secret key (seed)."""
    a, _ = _expand(secret)
    return _compress(_mul(a, G))


def sign(secret, message):
    """The 64-byte signature of `message` (bytes) with a 32-byte secret key."""
    a, prefix = _expand(secret)
    A = _compress(_mul(a, G))
    r = _hash_modl(prefix + message)
    R = _compress(_mul(r, G))
    s = (r + _hash_modl(R + A + message) * a) % L
    return R + int.to_bytes(s, 32, "little")


def verify(public, message, signature):
    """True when `signature` (64 bytes) is a valid signature of `message` under `public` (32 bytes)."""
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    R = _decompress(signature[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False                       # non-canonical: refused, as RFC 8032 requires
    h = _hash_modl(signature[:32] + public + message)
    return _equal(_mul(s, G), _add(R, _mul(h, A)))
