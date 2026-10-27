"""HTTPS for the panel on the local network: a private CA (made once, to be installed on the iPhone / the PC) and a server certificate
for the PC's addresses, signed by it. iOS accepts a server certificate for at most 825 days and wants the IP in subjectAltName.
Files are in server\\certs\\: ca.crt (public, the panel hands it out), ca.key (never leaves the PC), server.crt, server.key."""
import datetime as dt
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DIR = os.path.join(os.environ.get("LOSTOS_DATA", os.path.dirname(os.path.abspath(__file__))), "certs")
CA_CRT, CA_KEY = os.path.join(DIR, "ca.crt"), os.path.join(DIR, "ca.key")
SRV_CRT, SRV_KEY = os.path.join(DIR, "server.crt"), os.path.join(DIR, "server.key")
SERVER_DAYS = 800


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_key(path: str, key):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))


def _name(cn: str):
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "lostOS"), x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca():
    if os.path.exists(CA_CRT) and os.path.exists(CA_KEY):
        return (x509.load_pem_x509_certificate(open(CA_CRT, "rb").read()),
                serialization.load_pem_private_key(open(CA_KEY, "rb").read(), None))
    key = _key()
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name("lostOS local CA")).issuer_name(_name("lostOS local CA")).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    os.makedirs(DIR, exist_ok=True)
    open(CA_CRT, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    _write_key(CA_KEY, key)
    return cert, key


def _wanted(ips) -> set:
    return {str(i) for i in ips if i} | {"127.0.0.1"}


def _server_ok(ips) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(open(SRV_CRT, "rb").read())
        have = {str(i) for i in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)}
        return _wanted(ips) <= have and cert.not_valid_after_utc > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)
    except Exception:
        return False


def ensure(ips) -> tuple:
    """(server cert, server key, ca cert) paths; makes or renews what is missing (the PC's address changed, the certificate runs out)."""
    ca_cert, ca_key = _ca()
    if not (os.path.exists(SRV_KEY) and _server_ok(ips)):
        wanted = sorted(_wanted(ips))
        key = _key()
        now = dt.datetime.now(dt.timezone.utc)
        names = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname()), x509.DNSName("lostos.local")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in wanted]
        cert = (x509.CertificateBuilder().subject_name(_name("lostOS panel")).issuer_name(ca_cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
                .add_extension(x509.SubjectAlternativeName(names), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, content_commitment=False, data_encipherment=False, key_agreement=False,
                                             key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        open(SRV_CRT, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
        _write_key(SRV_KEY, key)
    return SRV_CRT, SRV_KEY, CA_CRT


def lan_ips() -> list:
    """The PC's IPv4 addresses on the local network (the address the speaker sees is one of them)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


if __name__ == "__main__":
    c, k, ca = ensure(lan_ips())
    print("server cert:", c, "\nca cert:", ca, "\naddresses:", lan_ips())
