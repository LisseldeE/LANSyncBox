"""
TLS 传输层
对全部 TCP 链路做传输加密且绝不回退明文，只加密、不校验证书身份（CERT_NONE）。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import socket
import ssl
import threading
from pathlib import Path
from typing import Optional, Tuple

from config import Config, UserConfig

# 握手超时（秒）：局域网内完成 TLS 握手足够
HANDSHAKE_TIMEOUT = 5.0
# 证书有效期（天）
_CERT_VALID_DAYS = 3650

_lock = threading.Lock()
_server_context: Optional[ssl.SSLContext] = None
_client_context: Optional[ssl.SSLContext] = None


def _cert_dir() -> Path:
    """证书目录（用户数据目录下的 tls/）"""
    return Config.get_data_dir() / "tls"


def _cert_files() -> Tuple[Path, Path]:
    base = _cert_dir()
    return base / "device.crt", base / "device.key"


def _atomic_write(path: Path, data: bytes):
    """原子写文件：先写临时文件再替换，避免生成中途被杀留下半成品"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _generate_certificate(cert_file: Path, key_file: Path):
    """生成自签 ECDSA P-256 证书与私钥（PEM）。"""
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError as e:  # cryptography 缺失：硬失败，不回退明文
        raise RuntimeError(f"缺少 cryptography 依赖，无法启用 TLS 加密: {e}") from e

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, UserConfig.get_end_id() or "LANSyncBox"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=_CERT_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    cert_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(cert_file, cert_pem)
    _atomic_write(key_file, key_pem)
    try:
        os.chmod(key_file, 0o600)  # 私钥仅本用户可读（Windows 上无实际作用）
    except OSError:
        pass


def _ensure_certificate() -> Tuple[Path, Path]:
    """确保本设备证书存在且可用；缺失或损坏则重建。返回 (cert_path, key_path)。"""
    cert_file, key_file = _cert_files()
    if cert_file.is_file() and key_file.is_file() and cert_file.stat().st_size > 0 \
            and key_file.stat().st_size > 0:
        return cert_file, key_file
    _generate_certificate(cert_file, key_file)
    return cert_file, key_file


def _build_server_context() -> ssl.SSLContext:
    cert_file, key_file = _ensure_certificate()
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert_file), str(key_file))
        return ctx
    except (ssl.SSLError, OSError, ValueError):
        # 已有文件损坏/不匹配：重建后重试一次
        _generate_certificate(cert_file, key_file)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert_file), str(key_file))
        return ctx


def get_server_context() -> ssl.SSLContext:
    """获取服务端 SSLContext（进程内单例）"""
    global _server_context
    with _lock:
        if _server_context is None:
            _server_context = _build_server_context()
        return _server_context


def get_client_context() -> ssl.SSLContext:
    """获取客户端 SSLContext（进程内单例）。

    仅加密、不校验证书身份：关闭 hostname 校验并置 CERT_NONE，
    与房间密码准入逻辑无关。
    """
    global _client_context
    with _lock:
        if _client_context is None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            # 顺序不可颠倒：check_hostname 为 True 时不允许置 CERT_NONE
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            _client_context = ctx
        return _client_context


def _wrap(sock, ctx: ssl.SSLContext, server_side: bool, host: Optional[str]) -> ssl.SSLSocket:
    """带超时的 TLS 握手封装：失败关闭底层 socket 并抛异常（不回退明文）。

    握手完成后恢复调用方原超时，由调用方按自身需要再设置。
    """
    old_timeout = sock.gettimeout()
    # 握手超时不得超过调用方原有超时，否则 1~2s 的快速失败会被拉长到 5s
    timeout = HANDSHAKE_TIMEOUT
    if old_timeout is not None and old_timeout < timeout:
        timeout = old_timeout
    sock.settimeout(timeout)
    wrapped = None
    try:
        wrapped = ctx.wrap_socket(
            sock,
            server_side=server_side,
            server_hostname=None if server_side else host,
            do_handshake_on_connect=False,
        )
        wrapped.do_handshake()
    except Exception:
        try:
            (wrapped or sock).close()
        except Exception:
            pass
        raise
    finally:
        try:
            (wrapped or sock).settimeout(old_timeout)
        except Exception:
            pass
    return wrapped


def wrap_server(sock):
    """服务端侧 TLS 封装（accept 得到的裸 socket）。

    必须在该连接的独立处理线程内调用，绝不能在 accept 循环内调用，
    否则握手超时会阻塞监听。
    """
    if isinstance(sock, ssl.SSLSocket):
        return sock
    return _wrap(sock, get_server_context(), server_side=True, host=None)


def wrap_client(sock, host: Optional[str] = None):
    """客户端侧 TLS 封装（connect 成功后的裸 socket）。

    host 仅作 SNI 用途；CERT_NONE 下不参与身份校验。
    """
    if isinstance(sock, ssl.SSLSocket):
        return sock
    return _wrap(sock, get_client_context(), server_side=False, host=host)


# ---- 设备身份（复用 TLS 证书/私钥做每连接签名） ----

def get_local_certificate_der() -> bytes:
    """本设备证书的 DER 字节（确保证书已生成）。"""
    get_server_context()  # 惰性生成/校验证书
    cert_file, _ = _ensure_certificate()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
    except ImportError as e:  # 与 TLS 一致：缺依赖硬失败
        raise RuntimeError(f"缺少 cryptography 依赖，无法校验证书身份: {e}") from e
    with open(cert_file, 'rb') as f:
        cert = x509.load_pem_x509_certificate(f.read())
    return cert.public_bytes(serialization.Encoding.DER)


def sign_with_local_key(data: bytes) -> bytes:
    """用本设备 TLS 私钥对 data 做 ECDSA-SHA256 签名（DER 编码）。"""
    get_server_context()
    _, key_file = _ensure_certificate()
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as e:
        raise RuntimeError(f"缺少 cryptography 依赖，无法校验证书身份: {e}") from e
    with open(key_file, 'rb') as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    return key.sign(data, ec.ECDSA(hashes.SHA256()))
