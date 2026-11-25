"""
手机访问服务（桌面端内嵌局域网 HTTP 服务）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import FileResponse, JSONResponse, Response

from config import Config
from sync.file_manager import safe_rmtree
from web.bridge import WebToolBridge

COOKIE_NAME = 'lsb_sid'
SESSION_TTL = 30 * 60          # 会话滑动过期（秒）
PORT_RANGE = (18600, 18700)    # 端口探测范围
MAX_UPLOAD_BYTES = 8 * 1024 ** 3
MAX_SEARCH_RESULTS = 200       # 搜索最多返回条数（与桌面端一致）
MAX_SEARCH_ENTRIES = 20000     # 搜索最多扫描条目数
MAX_SEARCH_SECONDS = 5.0       # 搜索最长时间预算
_ASSET_PATH = Path(__file__).resolve().parent / 'assets' / 'index.html'


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(str(ip or ''))
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def _hidden_name(name: str) -> bool:
    """页面不展示的条目：隐藏文件、临时文件、同步提示文件。"""
    lower = name.lower()
    return (name.startswith('.') or lower.endswith('.part')
            or lower.endswith('.tmp') or Config.is_sync_notice(name))


def _clean_name(name: str) -> str:
    """校验并返回安全的单层文件名。"""
    raw = str(name or '')
    if not raw or raw != raw.strip():
        raise HTTPException(400, '文件名无效')
    if raw in ('.', '..') or '/' in raw or '\\' in raw or ':' in raw:
        raise HTTPException(400, '文件名无效')
    if any(ch in raw for ch in '<>:"|?*') or any(ord(ch) < 32 for ch in raw):
        raise HTTPException(400, '文件名包含非法字符')
    if raw.startswith('.') or raw.endswith('.') or raw.endswith(' '):
        raise HTTPException(400, '文件名无效')
    return raw


def _unique_path(directory: str, name: str) -> str:
    """同名时自动追加 (n)。"""
    target = os.path.join(directory, name)
    if not os.path.exists(target):
        return target
    base, ext = os.path.splitext(name)
    n = 1
    while True:
        candidate = os.path.join(directory, f'{base} ({n}){ext}')
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _replace_file(tmp: str, target: str):
    """临时文件落盘：优先原子替换，跨盘时退化为复制后删除。"""
    try:
        os.replace(tmp, target)
    except OSError:
        shutil.copy2(tmp, target)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _remove_file(path: str):
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def clear_upload_cache() -> bool:
    """清空网页上传临时缓存（配置目录/web-cache）。

    上传中断或进程崩溃会留下 up-*.part；服务启动、停止与进入房间时各清一次，
    删除失败不抛错，避免影响主流程。
    """
    directory = Config.get_web_cache_dir()
    if not directory.is_dir():
        return True
    ok = True
    try:
        entries = list(directory.iterdir())
    except OSError:
        return False
    for entry in entries:
        try:
            if entry.is_dir():
                safe_rmtree(str(entry))
            else:
                _remove_file(str(entry))
        except OSError:
            ok = False
    return ok


class MobileWebService:
    """手机网页访问服务：启动/停止、鉴权、文件与私信 API。"""

    def __init__(self, room_root, room_code: str, is_host: bool, bridge=None,
                 *, on_added=None, on_deleted=None, on_renamed=None,
                 on_dir_created=None, chat_peers=None, chat_history=None,
                 chat_send=None, chat_read=None, room_state=None,
                 readonly=None, version: str = '', asset_path=None,
                 token: str = ''):
        self._root = os.path.realpath(str(room_root))
        self._room_code = str(room_code or '')
        self._is_host = bool(is_host)
        self._bridge = bridge
        self._on_added = on_added
        self._on_deleted = on_deleted
        self._on_renamed = on_renamed
        self._on_dir_created = on_dir_created
        self._chat_peers = chat_peers
        self._chat_history = chat_history
        self._chat_send = chat_send
        self._chat_read = chat_read
        self._room_state = room_state
        self._readonly = readonly
        self._version = version
        self._asset = Path(asset_path) if asset_path else _ASSET_PATH

        self._lock = threading.Lock()
        self._sessions = {}          # sid -> 过期时间
        self._fail_log = {}          # ip -> [失败时间]
        self._token = token          # 上层复用：重启服务后已打开的页面仍可凭新校验码重新进入
        self._code = ''              # 四位校验码：扫码后还需在网页输入
        self._port = 0
        self._uv = None
        self._thread = None
        # 上传缓存放在配置目录里，由程序统一清理（不占系统 temp）
        self._tmp_dir = str(Config.get_web_cache_dir())

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        return bool(self._uv is not None and self._thread and self._thread.is_alive())

    @property
    def port(self) -> int:
        return self._port

    @property
    def token(self) -> str:
        return self._token

    @property
    def session_count(self) -> int:
        now = time.time()
        with self._lock:
            expired = [sid for sid, exp in self._sessions.items() if exp < now]
            for sid in expired:
                self._sessions.pop(sid, None)
            return len(self._sessions)

    def start(self) -> dict:
        """启动服务并返回二维码所需信息（重复调用直接返回现状）。"""
        with self._lock:
            if self.running:
                return self._url_info()
            if not self._token:
                self._token = secrets.token_urlsafe(24)
            self._code = f'{secrets.randbelow(10000):04d}'
            self._sessions.clear()
            self._fail_log.clear()

        # 上次异常退出可能留下半截上传文件，开服前先清掉
        clear_upload_cache()
        port = self._pick_port()
        app = self._build_app()
        import uvicorn
        config = uvicorn.Config(app, host='0.0.0.0', port=port,
                                log_level='warning', access_log=False,
                                lifespan='off')
        server = uvicorn.Server(config)
        thread = threading.Thread(target=self._run_server, args=(server,),
                                  name='lsb-mobile-web', daemon=True)
        self._uv = server
        self._thread = thread
        thread.start()

        deadline = time.time() + 8.0
        while time.time() < deadline:
            if getattr(server, 'started', False):
                break
            if not thread.is_alive():
                break
            time.sleep(0.05)
        if not getattr(server, 'started', False):
            self.stop()
            raise RuntimeError('网页服务启动失败，端口可能被占用')
        self._port = port
        return self._url_info()

    def stop(self):
        """停止监听、作废 token 与全部会话。"""
        uv = self._uv
        thread = self._thread
        self._uv = None
        self._thread = None
        self._port = 0
        self._token = ''
        self._code = ''
        with self._lock:
            self._sessions.clear()
            self._fail_log.clear()
        if uv is not None:
            uv.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=4.0)
        # 关服时把仍在传的文件与历史残留一起清掉，再删除空目录
        clear_upload_cache()
        try:
            os.rmdir(self._tmp_dir)
        except OSError:
            pass

    @staticmethod
    def _run_server(server):
        try:
            asyncio.run(server.serve())
        except Exception:
            pass

    def _pick_port(self) -> int:
        start, end = PORT_RANGE
        for port in range(start, end):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(('0.0.0.0', port))
                return port
            except OSError:
                continue
            finally:
                sock.close()
        raise RuntimeError('没有可用的端口')

    @staticmethod
    def local_ips() -> list:
        """枚举本机非回环 IPv4，主出口地址排在最前。"""
        ips = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(('8.8.8.8', 80))
                ip = sock.getsockname()[0]
            finally:
                sock.close()
            if ip and not ip.startswith('127.'):
                ips.append(ip)
        except OSError:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET, socket.SOCK_DGRAM):
                ip = info[4][0]
                if ip and not ip.startswith('127.') and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
        return ips or ['127.0.0.1']

    def _url_info(self) -> dict:
        ips = self.local_ips()
        urls = [f'http://{ip}:{self._port}/#t={self._token}' for ip in ips]
        return {
            'port': self._port,
            'token': self._token,
            'code': self._code,
            'ips': ips,
            'urls': urls,
            'addresses': [f'http://{ip}:{self._port}/' for ip in ips],
            'primary_url': urls[0],
        }

    # ---------- 主线程调用 ----------

    def _call_main(self, fn, default=None):
        if fn is None:
            return default
        if self._bridge is None:
            return default
        return self._bridge.call(fn)

    def _notify(self, fn, *args):
        if fn is None or self._bridge is None:
            return
        self._bridge.call(lambda: fn(*args))

    def _chat_call(self, fn, default=None):
        """私信接口的容错调用：面板未就绪时返回默认值。"""
        if fn is None or self._bridge is None:
            return default
        return self._bridge.call(fn)

    # ---------- 鉴权 ----------

    def _client_ip(self, request: Request) -> str:
        return request.client.host if request.client else ''

    def _require(self, request: Request, write: bool = False):
        ip = self._client_ip(request)
        if not _is_private_ip(ip):
            raise HTTPException(403, '仅限局域网访问')
        sid = request.cookies.get(COOKIE_NAME)
        now = time.time()
        with self._lock:
            exp = self._sessions.get(sid) if sid else None
            if exp is None or exp < now:
                if sid:
                    self._sessions.pop(sid, None)
                raise HTTPException(401, '会话已失效，请重新扫码')
            self._sessions[sid] = now + SESSION_TTL
        if write:
            if request.headers.get('x-lsb') != '1':
                raise HTTPException(403, '非法请求')
            if self._readonly is not None and self._call_main(self._readonly, False):
                raise HTTPException(403, '当前房间为只读')

    def _rate_limited(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._fail_log.get(ip, []) if now - t < 60]
            hits.append(now)
            self._fail_log[ip] = hits
            return len(hits) > 10

    # ---------- 路径 ----------

    def _safe_rel(self, rel) -> str:
        raw = str(rel or '').replace('\\', '/').strip()
        if raw.startswith('/') or re.match(r'^[A-Za-z]:', raw):
            raise HTTPException(400, '非法路径')
        parts = [p for p in raw.split('/') if p]
        for part in parts:
            if (part in ('.', '..') or part.startswith('.')
                    or ':' in part
                    or any(ch in part for ch in '<>:"|?*')
                    or any(ord(ch) < 32 for ch in part)):
                raise HTTPException(400, '非法路径')
        target = os.path.realpath(os.path.join(self._root, *parts))
        if target != self._root and not target.startswith(self._root + os.sep):
            raise HTTPException(400, '非法路径')
        return target

    def _rel_of(self, abs_path: str) -> str:
        return os.path.relpath(abs_path, self._root).replace('\\', '/')

    # ---------- FastAPI ----------

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.exception_handler(HTTPException)
        async def _http_error(_request, exc):
            return JSONResponse({'ok': False, 'error': exc.detail},
                                status_code=exc.status_code)

        @app.middleware('http')
        async def _no_store(request, call_next):
            # 接口响应一律禁缓存：部分移动端浏览器会对无缓存头的 GET 做启发式缓存，
            # 导致轮询拿到旧目录快照、外部新增文件不刷新
            response = await call_next(request)
            if request.url.path.startswith('/api/'):
                response.headers['Cache-Control'] = 'no-store'
            return response

        @app.get('/')
        def index():
            if not self._asset.is_file():
                return Response('页面资源缺失', status_code=500, media_type='text/plain')
            return FileResponse(str(self._asset), media_type='text/html',
                                headers={'Cache-Control': 'no-store'})

        @app.get('/favicon.ico')
        def favicon():
            return Response(status_code=204)

        @app.post('/api/session')
        async def create_session(request: Request):
            ip = self._client_ip(request)
            if not _is_private_ip(ip):
                raise HTTPException(403, '仅限局域网访问')
            try:
                data = await request.json()
            except Exception:
                data = {}
            token = str((data or {}).get('t') or '')
            if not self._token or not hmac.compare_digest(token, self._token):
                if self._rate_limited(ip):
                    raise HTTPException(403, '尝试过于频繁，请稍后再扫码')
                raise HTTPException(403, '链接已失效，请重新扫码')
            # 四位校验码：只认桌面端二维码下方展示的数字，错误计入失败次数
            code = str((data or {}).get('code') or '').strip()
            if not code:
                return JSONResponse({'ok': False, 'needCode': True,
                                     'error': '请输入校验码'},
                                    status_code=403)
            if not self._code or not hmac.compare_digest(code, self._code):
                if self._rate_limited(ip):
                    return JSONResponse({'ok': False, 'needCode': True,
                                         'error': '尝试过于频繁，请稍后再试'},
                                        status_code=403)
                return JSONResponse({'ok': False, 'needCode': True,
                                     'error': '校验码错误'},
                                    status_code=403)
            sid = secrets.token_urlsafe(18)
            with self._lock:
                self._sessions[sid] = time.time() + SESSION_TTL
            resp = JSONResponse({'ok': True, 'room': self._room_code,
                                 'isHost': self._is_host})
            # 不设 max_age：浏览器关闭即失效，重新进入页面需再次输入校验码
            resp.set_cookie(COOKIE_NAME, sid,
                            httponly=True, samesite='strict', path='/')
            return resp

        @app.get('/api/info')
        def info(request: Request):
            self._require(request)
            state = self._call_main(self._room_state, {}) or {}
            info_data = {
                'ok': True,
                'room': self._room_code,
                'isHost': self._is_host,
                'role': 'host' if self._is_host else 'member',
                'devices': int(state.get('devices', 0) or 0),
                'readonly': bool(state.get('readonly', False)),
                'version': self._version,
                'port': self._port,
                'addresses': [f'http://{ip}:{self._port}/' for ip in self.local_ips()],
            }
            return info_data

        @app.get('/api/files')
        def files(request: Request, dir: str = ''):
            self._require(request)
            base = self._safe_rel(dir)
            if not os.path.isdir(base):
                raise HTTPException(404, '目录不存在')
            entries = []
            sig_parts = []
            try:
                with os.scandir(base) as it:
                    for entry in it:
                        name = entry.name
                        if _hidden_name(name):
                            continue
                        try:
                            st = entry.stat()
                            is_dir = entry.is_dir()
                        except OSError:
                            continue
                        item = {'name': name, 'dir': is_dir,
                                'size': 0 if is_dir else int(st.st_size),
                                'mtime': int(st.st_mtime * 1000)}
                        if is_dir:
                            item['count'] = self._count_children(entry.path)
                        entries.append(item)
                        sig_parts.append(f"{name}|{item['dir']}|{item['size']}|{item['mtime']}")
            except OSError as exc:
                raise HTTPException(500, f'读取目录失败: {exc}')
            entries.sort(key=lambda x: (not x['dir'], x['name'].casefold()))
            sig = hashlib.sha1('\n'.join(sorted(sig_parts)).encode('utf-8')).hexdigest()
            return {'ok': True, 'dir': dir.replace('\\', '/'), 'entries': entries, 'sig': sig}

        @app.get('/api/search')
        def search(request: Request, dir: str = '', q: str = ''):
            """递归搜索：与桌面端 FileSearchWorker 保持同一匹配规则与预算。"""
            self._require(request)
            keyword = (q or '').strip().lower()
            if not keyword:
                return {'ok': True, 'keyword': '', 'entries': [],
                        'scanned': 0, 'truncated': False}
            if len(keyword) > 100:
                raise HTTPException(400, '关键词过长')
            base = self._safe_rel(dir)
            if not os.path.isdir(base):
                raise HTTPException(404, '目录不存在')

            entries = []
            scanned = 0
            truncated = False
            deadline = time.monotonic() + MAX_SEARCH_SECONDS
            stack = [base]
            root = self._root

            while stack and not truncated:
                if len(entries) >= MAX_SEARCH_RESULTS:
                    truncated = True
                    break
                current = stack.pop()
                try:
                    iterator = os.scandir(current)
                except OSError:
                    continue
                try:
                    for entry in iterator:
                        scanned += 1
                        if scanned > MAX_SEARCH_ENTRIES:
                            truncated = True
                            break
                        name = entry.name
                        if _hidden_name(name):
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            is_file = entry.is_file(follow_symlinks=False)
                        except OSError:
                            continue
                        if not (is_dir or is_file):
                            continue
                        # 先入栈，命中文件夹时其子项也要被扫描
                        if is_dir:
                            stack.append(entry.path)
                        if keyword not in name.lower():
                            continue
                        size = 0
                        mtime = 0
                        if is_file:
                            try:
                                st = entry.stat(follow_symlinks=False)
                                size = int(st.st_size)
                                mtime = int(st.st_mtime * 1000)
                            except OSError:
                                pass
                        rel = os.path.relpath(entry.path, root).replace('\\', '/')
                        entries.append({
                            'name': name,
                            'dir': is_dir,
                            'size': size,
                            'mtime': mtime,
                            'path': rel,
                            'parent': os.path.dirname(rel).replace('\\', '/'),
                        })
                        if len(entries) >= MAX_SEARCH_RESULTS:
                            truncated = True
                            break
                except OSError:
                    continue
                finally:
                    try:
                        iterator.close()
                    except Exception:
                        pass
                if time.monotonic() > deadline:
                    truncated = True

            entries.sort(key=lambda x: (x['parent'].lower(), not x['dir'],
                                        x['name'].lower()))
            return {'ok': True, 'keyword': keyword, 'entries': entries,
                    'scanned': scanned, 'truncated': truncated}

        @app.put('/api/upload')
        async def upload(request: Request, dir: str = '', name: str = '',
                         overwrite: int = 0):
            self._require(request, write=True)
            base = self._safe_rel(dir)
            if not os.path.isdir(base):
                raise HTTPException(404, '目录不存在')
            fname = _clean_name(name)
            target = (os.path.join(base, fname) if overwrite
                      else _unique_path(base, fname))
            if overwrite and os.path.isdir(target):
                raise HTTPException(409, '同名项目是文件夹')
            os.makedirs(self._tmp_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='up-', suffix='.part', dir=self._tmp_dir)
            total = 0
            try:
                with os.fdopen(fd, 'wb') as fh:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            raise HTTPException(413, '文件超过大小上限')
                        fh.write(chunk)
                _replace_file(tmp, target)
            except HTTPException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            except Exception as exc:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise HTTPException(500, f'写入失败: {exc}')
            self._notify(self._on_added, target)
            return {'ok': True, 'name': os.path.basename(target), 'size': total}

        @app.get('/api/download')
        def download(request: Request, path: str = ''):
            self._require(request)
            target = self._safe_rel(path)
            if not os.path.isfile(target):
                raise HTTPException(404, '文件不存在')
            return FileResponse(target, filename=os.path.basename(target),
                                media_type='application/octet-stream',
                                headers={'X-Content-Type-Options': 'nosniff'})

        @app.post('/api/rename')
        async def rename(request: Request):
            self._require(request, write=True)
            data = await self._json(request)
            old = self._safe_rel(data.get('path'))
            if old == self._root or not os.path.exists(old):
                raise HTTPException(404, '项目不存在')
            new_name = _clean_name(data.get('newName'))
            new = os.path.join(os.path.dirname(old), new_name)
            if os.path.normcase(new) == os.path.normcase(old):
                return {'ok': True, 'name': new_name}
            if os.path.exists(new):
                raise HTTPException(409, '已存在同名项目')
            try:
                os.rename(old, new)
            except OSError as exc:
                raise HTTPException(500, f'重命名失败: {exc}')
            self._notify(self._on_renamed, old, new)
            return {'ok': True, 'name': new_name}

        @app.post('/api/delete')
        async def delete(request: Request):
            self._require(request, write=True)
            data = await self._json(request)
            paths = data.get('paths') or []
            if not isinstance(paths, list) or not paths:
                raise HTTPException(400, '未选择项目')
            removed = []
            try:
                for rel in paths:
                    target = self._safe_rel(rel)
                    if target == self._root or not os.path.exists(target):
                        continue
                    if os.path.isdir(target):
                        safe_rmtree(target)
                    else:
                        _remove_file(target)
                    removed.append(target)
            except OSError as exc:
                raise HTTPException(500, f'删除失败: {exc}')
            for target in removed:
                self._notify(self._on_deleted, target)
            return {'ok': True, 'count': len(removed)}

        @app.post('/api/mkdir')
        async def mkdir(request: Request):
            self._require(request, write=True)
            data = await self._json(request)
            base = self._safe_rel(data.get('dir'))
            if not os.path.isdir(base):
                raise HTTPException(404, '目录不存在')
            name = _clean_name(data.get('name'))
            target = os.path.join(base, name)
            if os.path.exists(target):
                raise HTTPException(409, '已存在同名项目')
            try:
                os.mkdir(target)
            except OSError as exc:
                raise HTTPException(500, f'创建失败: {exc}')
            self._notify(self._on_dir_created, target)
            return {'ok': True, 'name': name}

        @app.post('/api/move')
        async def move(request: Request):
            self._require(request, write=True)
            data = await self._json(request)
            dest = self._safe_rel(data.get('dest'))
            if not os.path.isdir(dest):
                raise HTTPException(404, '目标文件夹不存在')
            paths = data.get('paths') or []
            if not isinstance(paths, list) or not paths:
                raise HTTPException(400, '未选择项目')
            moved = []
            try:
                for rel in paths:
                    src = self._safe_rel(rel)
                    if not os.path.exists(src) or src == self._root:
                        continue
                    if os.path.dirname(src) == dest:
                        continue
                    if os.path.isdir(src):
                        if dest == src or dest.startswith(src + os.sep):
                            raise HTTPException(400, '不能移动到自身内部')
                    target = _unique_path(dest, os.path.basename(src))
                    os.rename(src, target)
                    moved.append((src, target))
            except HTTPException:
                raise
            except OSError as exc:
                raise HTTPException(500, f'移动失败: {exc}')
            for old, new in moved:
                self._notify(self._on_renamed, old, new)
            return {'ok': True, 'count': len(moved)}

        @app.get('/api/chat/peers')
        def chat_peers(request: Request):
            self._require(request)
            peers = self._call_main(self._chat_peers, []) or []
            return {'ok': True, 'peers': peers}

        @app.get('/api/chat/history')
        def chat_history(request: Request, peer: str = ''):
            self._require(request)
            peer = self._peer_id(peer)
            messages = self._chat_call(lambda: self._chat_history(peer), []) or []
            return {'ok': True, 'messages': messages}

        @app.post('/api/chat/send')
        async def chat_send(request: Request):
            self._require(request, write=True)
            data = await self._json(request)
            peer = self._peer_id(data.get('peer'))
            text = str(data.get('text') or '').strip()
            if not text:
                raise HTTPException(400, '消息为空')
            if len(text) > 2000:
                raise HTTPException(400, '消息过长')
            ok = self._chat_call(lambda: self._chat_send(peer, text), False)
            if not ok:
                raise HTTPException(502, '发送失败，对端可能已离线')
            return {'ok': True}

        @app.post('/api/chat/read')
        async def chat_read(request: Request):
            self._require(request)
            data = await self._json(request)
            peer = self._peer_id(data.get('peer'))
            self._chat_call(lambda: self._chat_read(peer), None)
            return {'ok': True}

        return app

    async def _json(self, request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, '请求格式错误')
        if not isinstance(data, dict):
            raise HTTPException(400, '请求格式错误')
        return data

    @staticmethod
    def _peer_id(peer) -> str:
        value = str(peer or '')
        if not value or len(value) > 64 or not re.match(r'^[A-Za-z0-9._-]+$', value):
            raise HTTPException(400, '对端标识无效')
        return value

    def _count_children(self, path: str) -> int:
        count = 0
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if not _hidden_name(entry.name):
                        count += 1
        except OSError:
            return 0
        return count
