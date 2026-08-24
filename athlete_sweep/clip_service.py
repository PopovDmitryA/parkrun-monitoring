"""Общий CLIP на всех ботов: одна копия модели вместо копии в каждом процессе.

Зачем. `Clip()` создаётся при старте каждого прогона, поэтому каждый бот грузил
собственные веса ViT-B-32 и арены torch. Замер smaps_rollup на живом сервере:
RSS ~980 МБ на бота, из них 605 МБ Private_Dirty — между процессами не делится
почти ничего. На 12 ботах это ~7.4 ГБ продублированной модели, и упор в память
наступал раньше, чем в процессор (load average 1.23 на 12 потоков).

Здесь модель живёт в одном процессе, боты ходят к нему через unix-сокет.
Если сокета нет или сервис упал — клиент молча поднимает модель у себя,
как раньше. Это важнее экономии: парсинг не должен вставать из-за сервиса.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import struct
import sys
import threading
from io import BytesIO

DEFAULT_SOCK = os.getenv("PM_CLIP_SOCK") or "/run/pm-clip/clip.sock"
_HDR = struct.Struct(">I")
MAX_MSG = 32 << 20  # 9 плиток base64 ≈ 150 КБ; запас на всякий случай


def _send(sock: socket.socket, obj: dict) -> None:
    body = json.dumps(obj).encode()
    sock.sendall(_HDR.pack(len(body)) + body)


def _recv(sock: socket.socket) -> dict | None:
    """Читает одно сообщение. None — собеседник закрыл соединение."""
    buf = b""
    while len(buf) < _HDR.size:
        chunk = sock.recv(_HDR.size - len(buf))
        if not chunk:
            return None
        buf += chunk
    (n,) = _HDR.unpack(buf)
    if n > MAX_MSG:
        raise ValueError(f"сообщение {n} байт — больше предела {MAX_MSG}")
    parts, got = [], 0
    while got < n:
        chunk = sock.recv(min(65536, n - got))
        if not chunk:
            return None
        parts.append(chunk)
        got += len(chunk)
    return json.loads(b"".join(parts))


# ---------------------------------------------------------------- сервер

class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            try:
                req = _recv(self.request)
            except (OSError, ValueError):
                return
            if req is None:
                return
            try:
                _send(self.request, {"ok": True, "result": self.server.run(req)})
            except Exception as exc:  # noqa: BLE001 — ответить обязаны любой ценой
                try:
                    _send(self.request, {"ok": False, "error": repr(exc)[:300]})
                except OSError:
                    return


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str) -> None:
        super().__init__(path, _Handler)
        from athlete_sweep.waf_solver import Clip

        self._clip = Clip()
        # Одна модель на всех: torch считает под замком, иначе несколько ботов
        # разом раздёргают потоки и посчитают медленнее, чем по очереди.
        self._lock = threading.Lock()

    def run(self, req: dict) -> list:
        from PIL import Image

        imgs = [Image.open(BytesIO(base64.b64decode(t))).convert("RGB")
                for t in req["tiles"]]
        with self._lock:
            if req["op"] == "classify":
                return [[c, p] for c, p in self._clip.classify(imgs)]
            if req["op"] == "rank":
                return self._clip.rank_for(imgs, req["target"])
        raise ValueError(f"неизвестная операция {req['op']!r}")


def serve(path: str = DEFAULT_SOCK) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    srv = _Server(path)
    os.chmod(path, 0o660)
    print(f"clip-сервис слушает {path}", flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        if os.path.exists(path):
            os.unlink(path)


# ---------------------------------------------------------------- клиент

class ClipClient:
    """Подменяет Clip: те же classify/rank_for, но считает чужой процесс.

    При любой беде переключается на локальную модель и больше к сокету
    не возвращается — дёргать мёртвый сервис на каждой капче незачем.
    """

    def __init__(self, path: str = DEFAULT_SOCK) -> None:
        self.path = path
        self._sock: socket.socket | None = None
        self._local = None  # запасная локальная модель
        self._connect()

    def _connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(120)
        s.connect(self.path)
        self._sock = s

    def _fallback(self, why: str):
        if self._local is None:
            print(f"  clip-сервис недоступен ({why}) — поднимаю модель локально",
                  flush=True)
            from athlete_sweep.waf_solver import Clip

            self._local = Clip()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        return self._local

    def _ask(self, op: str, images: list, target: str = "") -> list:
        if self._sock is None:
            raise RuntimeError("нет соединения")
        tiles = []
        for im in images:
            b = BytesIO()
            im.save(b, format="PNG")
            tiles.append(base64.b64encode(b.getvalue()).decode())
        _send(self._sock, {"op": op, "tiles": tiles, "target": target})
        resp = _recv(self._sock)
        if resp is None:
            raise RuntimeError("сервис закрыл соединение")
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "без объяснения"))
        return resp["result"]

    def classify(self, images: list) -> list[tuple[str, float]]:
        if self._local is None:
            try:
                return [(c, float(p)) for c, p in self._ask("classify", images)]
            except Exception as exc:  # noqa: BLE001
                self._fallback(repr(exc)[:120])
        return self._local.classify(images)

    def rank_for(self, images: list, target: str) -> list[float]:
        if self._local is None:
            try:
                return [float(x) for x in self._ask("rank", images, target)]
            except Exception as exc:  # noqa: BLE001
                self._fallback(repr(exc)[:120])
        return self._local.rank_for(images, target)


def make_clip():
    """Общий сервис, если он поднят; иначе — прежнее поведение, модель у себя."""
    from athlete_sweep.waf_solver import Clip

    path = DEFAULT_SOCK
    if not os.path.exists(path):
        return Clip()
    try:
        return ClipClient(path)
    except OSError as exc:
        print(f"  сокет {path} есть, но не отвечает ({exc}) — модель локально",
              flush=True)
        return Clip()


if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOCK)
