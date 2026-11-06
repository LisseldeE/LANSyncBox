"""
设备身份校验
TOFU 指纹信任表 + 每连接 nonce 签名，首见即固定、指纹不符即拒绝。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import base64
import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from config import Config, UserConfig
from network import tls_transport
from network.protocol import MessageReceiver, MessageType, Protocol

# 身份握手超时（秒）
IDENTITY_TIMEOUT = 5.0

# 证书 DER / end_id 上限（防恶意超长内容）
MAX_CERT_DER = 16 * 1024
MAX_END_ID = 200

_pin_lock = threading.Lock()
_pins_cache: Optional[dict] = None


class IdentityError(Exception):
    """身份校验失败（握手失败/签名不符/信任表损坏）。"""


class IdentityConflict(IdentityError):
    """已固定的 end_id 出示了不同证书指纹（疑似冒充）。"""


class PeerIdentity:
    """一次连接确认的对端身份（end_id + 证书指纹）。"""
    __slots__ = ('end_id', 'fingerprint')

    def __init__(self, end_id: str, cert_fp: str):
        self.end_id = end_id
        self.fingerprint = cert_fp

    def __repr__(self):
        return f"PeerIdentity({self.end_id!r}, {self.fingerprint[:12]})"


def fingerprint(cert_der: bytes) -> str:
    """证书 DER 的 SHA-256 指纹（小写十六进制）。"""
    return hashlib.sha256(cert_der).hexdigest()


def _pins_path() -> Path:
    # 测试可经环境变量隔离信任表，避免污染真实设备信任记录
    override = os.environ.get('LANSYNCBOX_TLS_PINS')
    if override:
        return Path(override)
    return Config.get_data_dir() / "tls" / "peers.json"


def _load_pins_locked() -> dict:
    """加载信任表（调用方须持锁）。损坏时硬失败，绝不静默重置信任。"""
    global _pins_cache
    if _pins_cache is not None:
        return _pins_cache
    path = _pins_path()
    pins = {}
    if path.is_file():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            raw = data.get('pins', data) if isinstance(data, dict) else {}
            if isinstance(raw, dict):
                pins = {str(k): str(v) for k, v in raw.items() if k and v}
        except (OSError, ValueError, TypeError) as e:
            raise IdentityError(f"设备信任表损坏: {path} ({e})") from e
    _pins_cache = pins
    return pins


def _save_pins_locked(pins: dict):
    """原子写信任表（调用方须持锁）。"""
    path = _pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'version': 1, 'pins': pins},
                         ensure_ascii=False, sort_keys=True).encode('utf-8')
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'wb') as f:
        f.write(payload)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def check_pin(end_id: str, cert_fp: str):
    """只校验指纹、不写入信任表（用于握手阶段，避免未认证连接抢占固定）。

    已固定但指纹不一致 -> 抛 IdentityConflict；未固定则放行，待业务认证通过后再 commit。
    """
    end_id = str(end_id or '')
    if not end_id:
        raise IdentityError("对端未提供 end_id")
    with _pin_lock:
        pins = _load_pins_locked()
        known = pins.get(end_id)
        if known and known != cert_fp:
            raise IdentityConflict(
                f"设备身份冲突: {end_id} 指纹 {cert_fp[:12]} != 已记录 {known[:12]}")


def commit_pin(end_id: str, cert_fp: str):
    """业务认证通过后固定指纹（首次 TOFU）；已固定则要求一致。"""
    end_id = str(end_id or '')
    if not end_id:
        raise IdentityError("对端未提供 end_id")
    with _pin_lock:
        pins = _load_pins_locked()
        known = pins.get(end_id)
        if known:
            if known != cert_fp:
                raise IdentityConflict(
                    f"设备身份冲突: {end_id} 指纹 {cert_fp[:12]} != 已记录 {known[:12]}")
            return
        pins[end_id] = cert_fp
        _save_pins_locked(pins)


def list_pins() -> dict:
    """返回信任表副本（end_id -> 指纹），供设置界面展示。"""
    with _pin_lock:
        return dict(_load_pins_locked())


def remove_pin(end_id: str) -> bool:
    """删除一条信任记录，返回是否确有删除。

    语义是"遗忘"而非"拉黑"：对端下次连接会按 TOFU 重新登记。
    """
    end_id = str(end_id or '')
    if not end_id:
        return False
    with _pin_lock:
        pins = _load_pins_locked()
        if end_id not in pins:
            return False
        del pins[end_id]
        _save_pins_locked(pins)
        return True


def pin_or_verify(end_id: str, cert_fp: str):
    """校验并固定（运维/测试便捷入口）。"""
    check_pin(end_id, cert_fp)
    commit_pin(end_id, cert_fp)


def verify_server_certificate(sock) -> PeerIdentity:
    """拨号方校验服务端 TLS 证书指纹，返回对端身份（校验不写入）。"""
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise IdentityError("对端未提供 TLS 证书")
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
    except ImportError as e:
        raise RuntimeError(f"缺少 cryptography 依赖，无法校验证书身份: {e}") from e
    try:
        cert = x509.load_der_x509_certificate(der)
        attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        end_id = attrs[0].value if attrs else ''
    except Exception as e:
        raise IdentityError(f"无法解析对端证书: {e}") from e
    if not end_id:
        raise IdentityError("对端证书缺少 CN（end_id）")
    cert_fp = fingerprint(der)
    check_pin(end_id, cert_fp)
    return PeerIdentity(end_id, cert_fp)


# ---- 身份握手帧 ----

def _pack(msg_type: int, data: dict) -> bytes:
    return Protocol._pack_json(msg_type, data)


def _send(sock, msg_type: int, data: dict):
    sock.sendall(_pack(msg_type, data))


def _read(sock, receiver: MessageReceiver, expected_type: int, deadline: float) -> bytes:
    """在 deadline 前读到指定类型的身份帧，返回原始 content。"""
    while time.time() < deadline:
        if receiver.has_complete_message():
            message = receiver.get_message()
            msg_type, _filename, _size, _mtime, _hide, content = message
            if msg_type == expected_type:
                return content if isinstance(content, bytes) else b''
            raise IdentityError(f"身份握手收到意外消息类型 0x{msg_type:02X}")
        try:
            data = sock.recv(65536)
        except socket.timeout as e:
            raise IdentityError("身份校验超时") from e
        if not data:
            raise IdentityError("对端在身份校验期间关闭连接")
        try:
            receiver.feed(data)
        except Exception as e:
            raise IdentityError(f"身份帧解析失败: {e}") from e
    raise IdentityError("身份校验超时")


def _json_content(content: bytes) -> dict:
    try:
        data = json.loads(content.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as e:
        raise IdentityError(f"身份帧内容非法: {e}") from e
    if not isinstance(data, dict):
        raise IdentityError("身份帧内容不是对象")
    return data


def _signed_payload(nonce: bytes, purpose: str, end_id: str) -> bytes:
    return nonce + b'|' + purpose.encode('utf-8') + b'|' + end_id.encode('utf-8')


# ---- 双方握手 ----

def authenticate_dialer(sock, purpose: str,
                        end_id: Optional[str] = None) -> PeerIdentity:
    """拨号方：校验服务端证书 -> 读取 nonce -> 提交私钥签名 -> 等待 OK。

    end_id 为本端逻辑身份（网状多实例测试可传各自 end_id；None 取持久端身份）。
    返回服务端身份（不写信任表；调用方在业务认证通过后 commit_pin）。
    """
    old_timeout = sock.gettimeout()
    sock.settimeout(IDENTITY_TIMEOUT)
    try:
        server_identity = verify_server_certificate(sock)
        receiver = MessageReceiver()
        deadline = time.time() + IDENTITY_TIMEOUT
        challenge = _json_content(
            _read(sock, receiver, MessageType.IDENTITY_CHALLENGE, deadline))
        if challenge.get('purpose') != purpose:
            raise IdentityError("身份挑战用途不匹配")
        try:
            nonce = base64.b64decode(challenge.get('nonce', ''), validate=True)
        except Exception as e:
            raise IdentityError(f"身份挑战 nonce 非法: {e}") from e
        if len(nonce) < 16:
            raise IdentityError("身份挑战 nonce 过短")

        end_id = end_id or UserConfig.get_end_id()
        cert_der = tls_transport.get_local_certificate_der()
        sig = tls_transport.sign_with_local_key(
            _signed_payload(nonce, purpose, end_id))
        _send(sock, MessageType.IDENTITY_PROOF, {
            'end_id': end_id,
            'cert': base64.b64encode(cert_der).decode('ascii'),
            'sig': base64.b64encode(sig).decode('ascii'),
        })
        _read(sock, receiver, MessageType.IDENTITY_OK, deadline)
        return server_identity
    finally:
        try:
            sock.settimeout(old_timeout)
        except Exception:
            pass


def authenticate_acceptor(sock, purpose: str) -> PeerIdentity:
    """接受方：下发一次性 nonce -> 校验签名与证书指纹 -> 回 OK。

    返回拨号方身份（不写信任表；调用方在业务认证通过后 commit_pin）。
    """
    old_timeout = sock.gettimeout()
    sock.settimeout(IDENTITY_TIMEOUT)
    try:
        nonce = os.urandom(32)
        _send(sock, MessageType.IDENTITY_CHALLENGE, {
            'nonce': base64.b64encode(nonce).decode('ascii'),
            'purpose': purpose,
        })
        receiver = MessageReceiver()
        deadline = time.time() + IDENTITY_TIMEOUT
        proof = _json_content(
            _read(sock, receiver, MessageType.IDENTITY_PROOF, deadline))

        end_id = str(proof.get('end_id', '') or '')
        if not end_id or len(end_id) > MAX_END_ID:
            raise IdentityError("对端 end_id 非法")
        try:
            cert_der = base64.b64decode(proof.get('cert', ''), validate=True)
            sig = base64.b64decode(proof.get('sig', ''), validate=True)
        except Exception as e:
            raise IdentityError(f"身份证明编码非法: {e}") from e
        if len(cert_der) > MAX_CERT_DER:
            raise IdentityError("对端证书过长")

        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError as e:
            raise RuntimeError(f"缺少 cryptography 依赖，无法校验证书身份: {e}") from e

        try:
            cert = x509.load_der_x509_certificate(cert_der)
            public_key = cert.public_key()
            public_key.verify(sig, _signed_payload(nonce, purpose, end_id),
                              ec.ECDSA(hashes.SHA256()))
        except Exception as e:
            raise IdentityError(f"身份签名校验失败: {e}") from e

        cert_fp = fingerprint(cert_der)
        check_pin(end_id, cert_fp)
        _send(sock, MessageType.IDENTITY_OK, {})
        return PeerIdentity(end_id, cert_fp)
    finally:
        try:
            sock.settimeout(old_timeout)
        except Exception:
            pass


