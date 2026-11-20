#!/usr/bin/env python3
"""
Self-signed HTTPS certificate for the APPC tablet dashboard, made with the Python standard library only
(no openssl / cryptography needed, so it works the same on macOS, Linux, Windows and a Pi).

The certificate belongs to this app alone: its own RSA-2048 key, subject "APPC Tablet Dashboard (<host>)",
kept in the dashboard's data folder - nothing is shared with other services on the machine.

    make(names, ips, days) -> (cert_pem, key_pem, info)

X.509 v3, sha256WithRSAEncryption, subjectAltName for every name / address the dashboard is reached by,
basicConstraints CA:TRUE so the one file can also be added to a browser's / system's trusted list.
"""
import base64
import datetime
import hashlib
import ipaddress
import secrets

# ---------------------------------------------------------------- RSA key

_SMALL = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


def _probable_prime(n, rounds=40):
    for p in _SMALL:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        n = secrets.randbits(bits) | (3 << (bits - 2)) | 1   # top two bits set: p*q has full length
        if _probable_prime(n):
            return n


def _rsa(bits=2048, e=65537):
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        d = _inv(e, phi)
        return n, e, d, p, q


def _inv(a, m):   # modular inverse (Python < 3.8 has no pow(a, -1, m))
    g, x, y = m, 0, 1
    r = a
    while r:
        k = g // r
        g, r = r, g - k * r
        x, y = y, x - k * y
    return x % m


# ---------------------------------------------------------------- DER

def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, content):
    return bytes([tag]) + _len(len(content)) + content


def _int(n):
    b = n.to_bytes(n.bit_length() // 8 + 1, "big")   # leading 0 keeps it positive
    return _tlv(0x02, b)


def _seq(*items):
    return _tlv(0x30, b"".join(items))


def _set(*items):
    return _tlv(0x31, b"".join(items))


def _oid(dotted):
    p = [int(x) for x in dotted.split(".")]
    out = bytes([40 * p[0] + p[1]])
    for v in p[2:]:
        chunk = [v & 0x7f]
        v >>= 7
        while v:
            chunk.append(0x80 | (v & 0x7f))
            v >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, out)


def _utf8(s):
    return _tlv(0x0c, s.encode("utf-8"))


def _bits(b):
    return _tlv(0x03, b"\0" + b)


def _octets(b):
    return _tlv(0x04, b)


def _time(t):
    return _tlv(0x17, t.strftime("%y%m%d%H%M%SZ").encode()) if t.year < 2050 else \
        _tlv(0x18, t.strftime("%Y%m%d%H%M%SZ").encode())


NULL = b"\x05\x00"
SHA256_RSA = _seq(_oid("1.2.840.113549.1.1.11"), NULL)


def _name(cn):
    return _seq(_set(_seq(_oid("2.5.4.10"), _utf8("APPC Tablet Dashboard"))),
                _set(_seq(_oid("2.5.4.3"), _utf8(cn))))


def _ext(oid, value, critical=False):
    return _seq(_oid(oid), *([b"\x01\x01\xff"] if critical else []), _octets(value))


def _pem(kind, der):
    b = base64.b64encode(der).decode()
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (
        kind, "\n".join(b[i:i + 64] for i in range(0, len(b), 64)), kind)


# ---------------------------------------------------------------- certificate

def make(names, ips, days=800, host_label=""):
    """names: DNS names, ips: IP address strings. Returns (cert_pem, key_pem, info dict)."""
    names = [n for n in dict.fromkeys(names) if n]
    ips = [i for i in dict.fromkeys(ips) if i]
    n, e, d, p, q = _rsa()
    pub = _seq(_int(n), _int(e))
    spki = _seq(_seq(_oid("1.2.840.113549.1.1.1"), NULL), _bits(pub))
    now = datetime.datetime.utcnow().replace(microsecond=0) - datetime.timedelta(minutes=5)
    end = now + datetime.timedelta(days=days)
    cn = "APPC Tablet Dashboard" + (" (%s)" % host_label if host_label else "")
    san = b"".join([_tlv(0x82, x.encode("idna") if not x.isascii() else x.encode()) for x in names] +
                   [_tlv(0x87, ipaddress.ip_address(x).packed) for x in ips])
    exts = _seq(
        _ext("2.5.29.19", _seq(b"\x01\x01\xff"), critical=True),                     # basicConstraints CA:TRUE
        _ext("2.5.29.15", _tlv(0x03, b"\x02\xa4"), critical=True),                   # digitalSignature, keyEncipherment, keyCertSign
        _ext("2.5.29.37", _seq(_oid("1.3.6.1.5.5.7.3.1"))),                          # serverAuth
        _ext("2.5.29.17", _seq(san)),                                                # subjectAltName
        _ext("2.5.29.14", _octets(hashlib.sha1(pub).digest())),                      # subjectKeyIdentifier
    )
    serial = secrets.randbits(127) | (1 << 126)
    tbs = _seq(_tlv(0xa0, _int(2)), _int(serial), SHA256_RSA, _name(cn), _seq(_time(now), _time(end)),
               _name(cn), spki, _tlv(0xa3, exts))
    # RSASSA-PKCS1-v1_5 with SHA-256
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(tbs).digest()
    k = (n.bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    cert = _seq(tbs, SHA256_RSA, _bits(sig))
    key = _seq(_int(0), _int(n), _int(e), _int(d), _int(p), _int(q), _int(d % (p - 1)), _int(d % (q - 1)),
               _int(_inv(q, p)))
    fp = hashlib.sha256(cert).hexdigest().upper()
    info = {"subject": cn, "names": names + ips, "not_after": end.strftime("%Y-%m-%d"),
            "created": now.strftime("%Y-%m-%d"),
            "fingerprint": ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))}
    return _pem("CERTIFICATE", cert), _pem("RSA PRIVATE KEY", key), info


if __name__ == "__main__":
    import sys
    import time
    t = time.time()
    c, k, i = make(["localhost"] + sys.argv[1:], ["127.0.0.1"], host_label="test")
    open("test.crt", "w").write(c)
    open("test.key", "w").write(k)
    print(i, "%.1f s" % (time.time() - t))
