#!/usr/bin/env python3
"""Свой решатель визуальной капчи AWS WAF (без платных сервисов).

Что это за капча (замерено на живых показах 28.07.2026): после кнопки Begin
показывается задание «Choose all the <категория>» и сетка 3x3 фотографий,
отрисованная в один canvas. Категории — бытовые предметы, за 18 раундов
встретились семь: bags, beds, buckets, curtains, clocks, hats, chairs.

Почему НЕ база размеченных картинок: 162 показа дали 160 уникальных фотографий
(перцептивных дублей 0), то есть библиотека у AWS на тысячи снимков — «запомнил
и узнал» не сработает. Поэтому классифицируем СОДЕРЖИМОЕ готовой моделью CLIP:
ей не нужна разметка, она отвечает «что на фото» для обычных предметов.

Плитки всё равно копим в data/waf_captcha — и как материал для анализа, и чтобы
уточнять реальный размер библиотеки по мере накопления.

Запуск (через SSH-проброшенный порт выхода):
  python -m athlete_sweep.waf_solver http://127.0.0.1:10859 --rounds 20
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from collections import Counter

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "waf_captcha")
URL = "https://www.parkrun.org.uk/parkrunner/620/"
BARCODE = "(A620)"

# Категории, которые видели живьём + запас из той же бытовой области.
# CLIP выбирает argmax по этому списку, поэтому важна полнота, а не порядок.
CATEGORIES = [
    "hat", "bed", "bag", "chair", "bucket", "clock", "curtain",
    "shoe", "lamp", "table", "sofa", "mirror", "pillow", "basket",
    "bottle", "cup", "plate", "towel", "book", "box", "car", "bicycle",
    "flower", "tree", "dog", "cat", "phone", "laptop", "watch", "glasses",
]
PROMPT = "a photo of a {}"
# Замерено на живых показах: правильных плиток всегда 5 из 9.
EXPECTED_PICKS = 5
# Сбор плиток в data/waf_captcha выключен: библиотека AWS оказалась в десятки
# тысяч снимков, накопление смысла не имеет (решаем классификатором, не базой).
SAVE_TILES = os.getenv("PM_WAF_SAVE_TILES", "") == "1"

JS_TILES = r"""
() => {
  const all = [];
  const walk = (root) => {
    root.querySelectorAll("*").forEach(el => {
      if (el.shadowRoot) walk(el.shadowRoot);
      if (el.tagName === "CANVAS") all.push(el);
    });
  };
  walk(document);
  if (!all.length) return null;
  const c = all.sort((a,b) => (b.width*b.height) - (a.width*a.height))[0];
  const r = c.getBoundingClientRect();
  const tw = Math.floor(c.width/3), th = Math.floor(c.height/3);
  const tiles = [];
  for (let row = 0; row < 3; row++)
    for (let col = 0; col < 3; col++) {
      const t = document.createElement("canvas");
      t.width = tw; t.height = th;
      t.getContext("2d").drawImage(c, col*tw, row*th, tw, th, 0, 0, tw, th);
      tiles.push(t.toDataURL());
    }
  // геометрия на экране — чтобы кликать по центрам плиток
  return {tiles, box: {x: r.x, y: r.y, w: r.width, h: r.height}};
}
"""


class Clip:
    """Обёртка над CLIP: отвечает, к какой категории ближе картинка."""

    def __init__(self) -> None:
        try:
            import open_clip
            import torch
        except ImportError as exc:
            raise SystemExit(
                f"\n!! Не установлена модель распознавания капчи ({exc.name}).\n"
                "   Ставить так (torch — ОБЯЗАТЕЛЬНО с cpu-индексом, иначе pip\n"
                "   потянет 2.5 ГБ сборки под видеокарту и часто падает):\n\n"
                "     py -m pip install torch --index-url https://download.pytorch.org/whl/cpu\n"
                "     py -m pip install open_clip_torch playwright\n"
                "     py -m playwright install chromium\n"
            ) from exc

        self.torch = torch
        self.model, _, self.pre = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k")
        self.model.eval()
        tok = open_clip.get_tokenizer("ViT-B-32")
        with torch.no_grad():
            t = tok([PROMPT.format(c) for c in CATEGORIES])
            self.text = self.model.encode_text(t)
            self.text /= self.text.norm(dim=-1, keepdim=True)

    def _probs(self, images: list):
        import torch

        with torch.no_grad():
            batch = torch.stack([self.pre(im) for im in images])
            feats = self.model.encode_image(batch)
            feats /= feats.norm(dim=-1, keepdim=True)
            return (100.0 * feats @ self.text.T).softmax(dim=-1)

    def classify(self, images: list) -> list[tuple[str, float]]:
        """Для каждой картинки: (лучшая категория, уверенность 0..1)."""
        out = []
        for row in self._probs(images):
            i = int(row.argmax())
            out.append((CATEGORIES[i], float(row[i])))
        return out

    def rank_for(self, images: list, target: str) -> list[float]:
        """Насколько каждая картинка похожа на ЦЕЛЕВУЮ категорию (не argmax).

        Нужно, когда argmax промахивается: 'bucket' модель порой зовёт 'cup',
        и тогда выбирается меньше плиток, чем надо. Ранжирование по цели даёт
        корректный порядок даже при таких соседних понятиях.
        """
        if target not in CATEGORIES:
            return [0.0] * len(images)
        j = CATEGORIES.index(target)
        return [float(row[j]) for row in self._probs(images)]


def _cred_env(env_key: str, *file_keys: str, default: str = "") -> str:
    """Креды: сначала переменная окружения, потом .env рядом и в saturday_runs_stats."""
    v = os.getenv(env_key, "")
    if v:
        return v
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = os.path.expanduser("~")
    for path in (os.path.join(root, ".env"),
                 os.path.join(home, "Projects", "saturday_runs_stats", ".env")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    for k in file_keys:
                        if line.startswith(k + "="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return default


def proxy_label(pxy: str) -> str:
    """Короткая метка прокси для табло: из net-212-119-47-225.mcccx.com → 212.119.47.225,
    иначе — просто хост."""
    host = pxy.split("://")[-1].split("@")[-1].split(":")[0]
    m = re.search(r"(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})", host)
    return ".".join(m.groups()) if m else host


def singular(word: str) -> str:
    w = word.lower().rstrip()
    for plural, single in (("ies", "y"), ("ses", "s"), ("s", "")):
        if w.endswith(plural) and len(w) > len(plural) + 1:
            return w[: -len(plural)] + single
    return w


def save_tiles(round_no: int, cat: str, raws: list[bytes]) -> int:
    """Копим библиотеку: дедуп по перцептивному хэшу + раскладка по раундам."""
    from io import BytesIO

    import imagehash
    from PIL import Image

    if not SAVE_TILES:
        return 0
    os.makedirs(f"{DATA}/library", exist_ok=True)
    d = f"{DATA}/rounds/{round_no:03d}_{cat}"
    os.makedirs(d, exist_ok=True)
    new = 0
    for pos, raw in enumerate(raws, 1):
        open(f"{d}/tile{pos}.png", "wb").write(raw)
        ph = str(imagehash.phash(Image.open(BytesIO(raw))))
        p = f"{DATA}/library/{ph}.png"
        if not os.path.exists(p):
            open(p, "wb").write(raw)
            new += 1
    return new


def next_round_no() -> int:
    d = f"{DATA}/rounds"
    if not os.path.isdir(d):
        return 1
    ns = [int(x[:3]) for x in os.listdir(d) if x[:3].isdigit()]
    return (max(ns) + 1) if ns else 1


def _wait_for(pg, predicate, timeout: float = 25.0, step: float = 0.5) -> bool:
    """Ждать РЕАЛЬНОГО появления элемента вместо фиксированной паузы.

    Замер 28.07: Мак (канал через VPN) проходил 79% капч, Windows (прямой
    канал) — 96%. Модель одна, значит дело было не в распознавании: по
    медленному каналу головоломка не успевала отрисоваться за жёсткие 6с,
    solve_once читал пустую страницу, возвращал «не нашёл задание», и капча
    записывалась в проваленные.
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def solve_once(pg, clip: Clip, round_no: int, verbose: bool = True) -> dict:
    """Один заход: снять головоломку, классифицировать, прокликать, подтвердить."""
    from io import BytesIO

    from PIL import Image

    info: dict = {"round": round_no}
    txt = pg.inner_text("body")[:3000]
    m = re.search(r"Choose all\s+(?:the\s+)?([a-zA-Z]+)", txt)
    if not m:
        info["error"] = "не нашёл задание"
        return info
    cat_plural = m.group(1).lower()
    target = singular(cat_plural)
    info["category"] = cat_plural

    data = pg.evaluate(JS_TILES)
    if not data:
        info["error"] = "canvas не найден"
        return info
    raws = [base64.b64decode(t.split(",", 1)[1]) for t in data["tiles"]]
    info["new_in_library"] = save_tiles(round_no, cat_plural, raws)

    imgs = [Image.open(BytesIO(r)).convert("RGB") for r in raws]
    preds = clip.classify(imgs)
    picked = [i for i, (c, _) in enumerate(preds) if c == target]

    # Замер на живых показах: правильных плиток ВСЕГДА 5 из 9 (проверено на 14
    # головоломках подряд). Значит иное число — признак промаха классификатора
    # (характерный случай: bucket модель зовёт cup). Тогда не гадаем, а берём
    # топ-5 по близости к самой ЦЕЛИ.
    fallback = len(picked) != EXPECTED_PICKS
    if fallback:
        scores = clip.rank_for(imgs, target)
        picked = sorted(range(len(imgs)), key=lambda i: -scores[i])[:EXPECTED_PICKS]
    info["preds"] = [f"{c}:{p:.2f}" for c, p in preds]
    info["picked"] = picked
    info["fallback"] = fallback
    if verbose:
        print(f"    задание: «{cat_plural}» → цель '{target}'"
              f"{'  [топ-5 по цели: argmax дал не 5]' if fallback else ''}", flush=True)
        for i, (c, p) in enumerate(preds):
            mark = "✔" if i in picked else " "
            print(f"      {mark} плитка {i+1}: {c} ({p:.2f})", flush=True)

    if not picked:
        info["error"] = "модель не нашла ни одной подходящей"
        return info

    # клики по центрам выбранных плиток
    box = data["box"]
    tw, th = box["w"] / 3, box["h"] / 3
    for i in picked:
        row, col = divmod(i, 3)
        pg.mouse.click(box["x"] + col * tw + tw / 2, box["y"] + row * th + th / 2)
        time.sleep(0.3)

    for sel in ["button:has-text('Confirm')", "text=Confirm"]:
        try:
            el = pg.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=5000)
                break
        except Exception:
            continue
    # Ждём реакции страницы (новая головоломка или её исчезновение), до 25с.
    before = txt[:400]
    _wait_for(pg, lambda: pg.inner_text("body")[:400] != before, timeout=25)
    time.sleep(1)
    # Читаем содержимое с повторами: pg.content() бросает «page is navigating»,
    # если страница переходит прямо в этот момент. А переходит она ровно тогда,
    # когда капчу ПРИНЯЛИ и WAF отправляет нас на настоящий контент. Голый вызов
    # ронял всю добычу токена, и успешно решённая капча записывалась в провал
    # (наблюдалось на сервере: CLIP выбрал плитки, клики прошли, дальше падение).
    body = ""
    for _ in range(8):
        try:
            body = pg.content()
            break
        except Exception:
            time.sleep(1)
    if not body:
        # Восемь секунд страница только и делала, что переходила. Считаем это
        # признаком пройденной капчи: провалившаяся никуда не ведёт, а показывает
        # следующую головоломку на месте.
        info["another_puzzle"] = False
        info["solved"] = True
        return info
    # Успех = капчи на странице БОЛЬШЕ НЕТ. Раньше проверяли по штрихкоду (A620),
    # но в рабочем режиме качаются другие атлеты — у них штрихкод свой, и условие
    # не выполнялось никогда: решённые капчи считались проваленными.
    info["another_puzzle"] = "Choose all" in body
    info["solved"] = not info["another_puzzle"] and "Human Verification" not in body
    return info


def pass_captcha(pg, clip: Clip, rn: int, max_puzzles: int = 6) -> tuple[bool, int, int]:
    """Провести страницу через капчу. Возвращает (прошли, решено_головоломок, rn)."""
    for sel in ["button:has-text('Begin')", "text=Begin"]:
        try:
            el = pg.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=5000)
                break
        except Exception:
            continue
    # Ждём, пока головоломка реально отрисуется (до 30с), а не гадаем паузой.
    if not _wait_for(pg, lambda: "Choose all" in pg.inner_text("body")[:3000], timeout=45):
        print("    головоломка не отрисовалась за 45с", flush=True)
    solved = 0
    for _ in range(max_puzzles):
        r = solve_once(pg, clip, rn); rn += 1
        if r.get("error"):
            return False, solved, rn
        solved += 1
        if r.get("solved"):
            return True, solved, rn
        if r.get("another_puzzle"):
            time.sleep(3)
            continue
        return False, solved, rn
    return False, solved, rn


def harvest_token(p, proxy: str, clip: Clip, rn: int, ua: str) -> tuple[str | None, int, str | None]:
    """Поднять браузер, пройти капчу, снять aws-waf-token и закрыться.

    Картинки НЕ блокируем: головоломка — это и есть картинки в canvas.
    UA тот же, что у httpx: WAF привязывает токен в том числе к нему.
    """
    # Прямой режим: ЯВНО отключаем прокси у Chromium (--no-proxy-server), иначе он
    # подхватывает СИСТЕМНЫЙ прокси Windows (напр. INCY) и падает
    # ERR_PROXY_CONNECTION_FAILED — даже когда мы прокси не задавали. httpx системный
    # прокси игнорирует, поэтому качал нормально, а браузер лез в него.
    if proxy:
        br = p.chromium.launch(headless=True, proxy={"server": proxy})
    else:
        br = p.chromium.launch(headless=True, args=["--no-proxy-server"])
    ctx = br.new_context(user_agent=ua, viewport={"width": 1280, "height": 900})
    # Ресурсы намеренно НЕ режем через ctx.route. Пробовали (шрифты/стили/медиа),
    # чтобы снизить число соединений через тоннель — выигрыша не увидели, а
    # Playwright при этом гонит каждый запрос через Python-колбэк, что по
    # медленному каналу подозрительно замедляло заход. Обрыв SSH всё равно
    # лечится пересозданием тоннеля (ensure_tunnel), а не экономией запросов.
    # Картинки резать нельзя в принципе: головоломка — это картинки в canvas.
    pg = ctx.new_page()
    token = None
    captcha_ok = True
    close_fail: str | None = None
    try:
        pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
        _wait_for(pg, lambda: any(m in pg.content() for m in
                                  ("Human Verification", "Choose all", BARCODE)), timeout=25)
        html = pg.content()
        if "Human Verification" in html or "Choose all" in html:
            captcha_ok, _n, rn = pass_captcha(pg, clip, rn)
        if captcha_ok:
            for c in ctx.cookies():
                if c["name"] == "aws-waf-token":
                    token = c["value"]
                    break
    except Exception as exc:
        print(f"    добыча токена не удалась: {exc!r}", flush=True)
    finally:
        # НЕ ctx.close(); br.close() одной строкой в общем try: если ctx.close()
        # кинет исключение, br.close() вообще не выполнится — сам процесс браузера
        # (+ его GPU/utility-подпроцессы) останется висеть навсегда. Обнаружено
        # 30.07 — за несколько часов накопилось 13 таких зависших chrome.exe
        # (~1.3 ГБ), одни и те же PID, память не менялась — то есть просто сидели.
        # Раздельные try + видимый лог вместо молчаливого pass — чтобы при
        # повторе было видно ПОЧЕМУ, а не гадать по Task Manager.
        try:
            ctx.close()
        except Exception as exc:
            print(f"    контекст браузера не закрылся: {exc!r}", flush=True)
        try:
            br.close()
        except Exception as exc:
            close_fail = repr(exc)[:200]
            print(f"    !! браузер НЕ закрылся, процесс мог остаться висеть: {exc!r}", flush=True)
    if not captcha_ok:
        return None, rn, close_fail
    return token, rn, close_fail


def work_fast(args) -> None:
    """БЫСТРЫЙ режим: страницы качает httpx (только HTML, без картинок и скриптов),
    браузер поднимается ТОЛЬКО когда прилетела капча — пройти её и отдать токен.

    Так на атлета уходит 1-2с вместо 8-9с у полностью браузерного прохода, а
    браузер включается примерно раз в 25 атлетов.
    """
    import sys as _sys

    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import httpx
    import psycopg
    from playwright.sync_api import sync_playwright

    from athlete_sweep.parse import AthleteData, parse_all_runs, parse_summary
    from athlete_sweep.worker import UA, WAF_MARKERS, claim, store

    def classify(status_code: int, headers, body: str) -> str:
        low = body[:2000].lower()
        if "x-amzn-waf-action" in {k.lower() for k in headers}:
            return "protected"
        if status_code in (403, 405) or any(m in low for m in WAF_MARKERS):
            return "protected"
        if status_code == 404:
            return "not_found"
        return "ok"

    def make_client(token: str | None) -> httpx.Client:
        # proxy=None → идём НАПРЯМУЮ каналом этой машины (свой IP/VPN).
        # Так быстрее: не гоняем трафик через SSH-тоннель к выходу сервера.
        return httpx.Client(
            proxy=(args.proxy or None), timeout=25.0, follow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"},
            cookies={"aws-waf-token": token} if token else {})

    ensure_tunnel = getattr(args, "ensure_tunnel", lambda: True)
    conn = psycopg.connect(args.dsn, autocommit=False, connect_timeout=10)

    def db(fn):
        """Операция с БД. При обрыве сначала чиним ТОННЕЛЬ (он и есть причина —
        порт 5433 идёт через него), потом переподключаемся и повторяем."""
        nonlocal conn
        try:
            return fn(conn)
        except psycopg.OperationalError:
            try:
                conn.close()
            except Exception:
                pass
            for _ in range(12):
                ensure_tunnel()
                try:
                    conn = psycopg.connect(args.dsn, autocommit=False, connect_timeout=10)
                    print("    БД: переподключился", flush=True)
                    return fn(conn)
                except psycopg.OperationalError:
                    time.sleep(5)
            raise RuntimeError("БД недоступна: тоннель не поднимается больше минуты")

    worker = f"waf-fast:{os.getpid()}"

    # Регистрация на табло /hq: без неё этот воркер работал мимо рейтинга.
    # Заводим ОТДЕЛЬНУЮ строку, а не пишем в строку самого выхода — иначе
    # собранное задвоилось бы со счётчиком серверного менеджера того же выхода.
    exit_label = str(getattr(args, "exit_port", 0) or "direct")
    try:
        row = db(lambda c: c.execute(
            "SELECT name FROM sweep_exits WHERE proxy LIKE %s AND account NOT IN ('free','mac')",
            (f"%:{exit_label}",)).fetchone())
        if row:
            exit_label = row[0]
    except Exception:
        pass
    import socket as _sock
    hostshort = _sock.gethostname().split('.')[0]
    if getattr(args, "exit_port", 0):
        board = f"mac+{exit_label}"
    elif args.proxy:
        # через прокси (в т.ч. локальный socks выхода) — метка по имени выхода из
        # файла, иначе по адресу; чтобы 5-6 терминалов не смешивали счётчики
        label = getattr(args, "proxy_label", "") or proxy_label(args.proxy)
        board = f"{hostshort}-{label}"
    else:
        board = f"{hostshort}-direct"
    db(lambda c: (c.execute(
        """INSERT INTO sweep_exits (name, proxy, kind, account, enabled, delay_sec,
                                    worker_heartbeat_at)
           VALUES (%s, 'mac-waf', 'mac', 'mac', true, %s, now())
           ON CONFLICT (name) DO UPDATE SET enabled=true, account='mac',
             delay_sec=EXCLUDED.delay_sec, cooldown_until=NULL, ban_level=0,
             worker_heartbeat_at=now()""", (board, args.delay)), c.commit()))

    def board_off() -> None:
        """Снять «работает» с табло на любом выходе из скрипта."""
        try:
            db(lambda c: (c.execute(
                "UPDATE sweep_exits SET worker_heartbeat_at=NULL, enabled=false "
                "WHERE name=%s", (board,)), c.commit()))
        except Exception:
            pass

    # Имя выхода в ЗАГОЛОВОК окна — видно страну прямо в тайтлбаре/таскбаре cmd,
    # не останавливая процесс (в истории консоли не найти, когда окон много).
    try:
        if os.name == "nt":
            os.system(f"title parkrun {board}")
        else:
            print(f"\033]0;parkrun {board}\007", end="", flush=True)
    except Exception:
        pass

    # Печать «через какой выход работало окно» — надёжно, через atexit: срабатывает
    # при ЛЮБОМ штатном завершении (Ctrl+C, лимит, пустая очередь), даже если
    # Playwright перехватил сигнал и обычный except KeyboardInterrupt не отработал.
    import atexit
    _exit_shown = {"v": False}

    def _print_exit_id() -> None:
        if _exit_shown["v"]:
            return
        _exit_shown["v"] = True
        where = args.proxy if args.proxy else "прямой канал (без прокси)"
        print(f"\n>>> ЭТО ОКНО РАБОТАЛО ЧЕРЕЗ: «{board}»  ·  {where}", flush=True)

    atexit.register(_print_exit_id)

    print("загружаю CLIP…", flush=True)
    clip = Clip()
    print(f"готово. воркер {worker} · на табло «{board}» · задержка {args.delay}с\n", flush=True)

    rn = next_round_no()
    token: str | None = None
    client = make_client(token)
    done = 0
    captchas = 0
    stats = Counter()
    t_start = time.time()
    with sync_playwright() as p:
        try:
            while not args.limit or done < args.limit:
                aid = db(lambda c: claim(c, worker, 60))
                if aid is None:
                    print("очередь пуста", flush=True)
                    break
                base = f"https://www.parkrun.org.uk/parkrunner/{aid}/"
                t0 = time.time()
                try:
                    r = client.get(base)
                    kind = classify(r.status_code, r.headers, r.text)
                    if kind == "protected":
                        captchas += 1
                        db(lambda c: (c.execute(
                            "UPDATE sweep_exits SET captcha_total=captcha_total+1, "
                            "last_captcha_at=now() WHERE name=%s", (board,)), c.commit()))
                        print(f"  капча на {aid} — поднимаю браузер за токеном…", flush=True)
                        token, rn, close_fail = harvest_token(p, args.proxy, clip, rn, UA)
                        if close_fail:
                            db(lambda c: (c.execute(
                                "UPDATE sweep_exits SET browser_close_fail_total="
                                "browser_close_fail_total+1, last_close_fail_at=now(), "
                                "last_close_fail_reason=%s WHERE name=%s",
                                (close_fail, board)), c.commit()))
                        if not token:
                            db(lambda c: (c.execute(
                                "UPDATE crawl_queue SET status='pending', claimed_by=NULL "
                                "WHERE athlete_id=%s", (aid,)), c.commit()))
                            stats["токен не добыт"] += 1
                            continue
                        stats["капча решена"] += 1
                        db(lambda c: (c.execute(
                            "UPDATE sweep_exits SET captcha_solved=captcha_solved+1 "
                            "WHERE name=%s", (board,)), c.commit()))
                        client.close(); client = make_client(token)
                        r = client.get(base)
                        kind = classify(r.status_code, r.headers, r.text)
                        if kind == "protected":
                            db(lambda c: (c.execute(
                                "UPDATE crawl_queue SET status='pending', claimed_by=NULL "
                                "WHERE athlete_id=%s", (aid,)), c.commit()))
                            stats["токен не помог"] += 1
                            continue
                    data = (AthleteData(status="not_found") if kind == "not_found"
                            else parse_summary(r.text, str(aid)))
                    if data.status == "ok":
                        r2 = client.get(base + "all/")
                        if classify(r2.status_code, r2.headers, r2.text) == "ok":
                            data.runs = parse_all_runs(r2.text, str(aid))
                    raw = r.text if data.status == "unclassified" else None
                    spent = int(time.time() - t0 + args.delay)
                    db(lambda c: (store(c, aid, data, raw), c.execute(
                        "UPDATE crawl_queue SET status=%s, claimed_by=NULL, fetched_at=now() "
                        "WHERE athlete_id=%s", (data.status, aid)), c.execute(
                        "UPDATE sweep_exits SET collected_total=collected_total+1, "
                        "active_seconds=active_seconds+%s, last_ok_at=now(), "
                        "worker_heartbeat_at=now() WHERE name=%s", (spent, board)), c.commit()))
                    done += 1
                    stats[data.status] += 1
                    print(f"  #{done} атлет {aid}: {data.name or data.status} "
                          f"({data.status}, {data.total_runs or 0} заб.) "
                          f"[{time.time()-t0:.1f}с]", flush=True)
                except (httpx.TransportError, httpx.HTTPError) as exc:
                    # Сеть/прокси отвалились — почти всегда это упавший тоннель.
                    # Чиним его и пересоздаём клиента, атлета возвращаем в очередь.
                    print(f"  атлет {aid}: сеть — {type(exc).__name__}, проверяю тоннель", flush=True)
                    ensure_tunnel()
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = make_client(token)
                    err = repr(exc)[:200]
                    db(lambda c: (c.execute(
                        "UPDATE crawl_queue SET status='pending', claimed_by=NULL, "
                        "attempts=attempts+1, error=%s WHERE athlete_id=%s", (err, aid)),
                        c.commit()))
                    stats["обрыв сети"] += 1
                except Exception as exc:
                    err = repr(exc)[:200]
                    db(lambda c: (c.execute(
                        "UPDATE crawl_queue SET status='pending', claimed_by=NULL, "
                        "attempts=attempts+1, error=%s WHERE athlete_id=%s", (err, aid)),
                        c.commit()))
                    stats["сбой"] += 1
                    print(f"  атлет {aid}: сбой {exc!r}", flush=True)
                time.sleep(args.delay)
        except KeyboardInterrupt:
            print("\nостановлено.", flush=True)
        finally:
            board_off()
            try:
                client.close(); conn.close()
            except Exception:
                pass
    el = time.time() - t_start
    _print_exit_id()  # выход окна — гарантированно (и через atexit тоже)
    print("=" * 46)
    print(f"АТЛЕТОВ записано: {done} за {el/60:.1f} мин "
          f"({el/max(done,1):.1f}с на атлета, ~{done/max(el,1)*3600:.0f}/час)")
    print(f"капч: {captchas}" + (f" (1 на {done/captchas:.0f} атлетов)" if captchas else ""))
    print("разбивка:", dict(stats))


def work_mode(args) -> None:
    """Полезный режим: берём атлетов из очереди, качаем через выход, при капче
    решаем её сами, парсим и пишем в БД. Токен живёт в контексте браузера, поэтому
    после одной решённой капчи подряд идёт много атлетов без единой новой."""
    import sys as _sys

    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import psycopg
    from playwright.sync_api import sync_playwright

    from athlete_sweep.parse import AthleteData, parse_all_runs, parse_summary
    from athlete_sweep.worker import claim, store

    dsn = args.dsn

    class Db:
        """Коннект с переподключением: SSH-тоннель к pm-postgres периодически
        рвётся и утаскивает соединение за собой — это не повод падать."""

        def __init__(self) -> None:
            self.conn = psycopg.connect(dsn, autocommit=False, connect_timeout=10)

        def reconnect(self) -> None:
            try:
                self.conn.close()
            except Exception:
                pass
            for attempt in range(12):
                try:
                    self.conn = psycopg.connect(dsn, autocommit=False, connect_timeout=10)
                    print("    БД: переподключился", flush=True)
                    return
                except Exception:
                    time.sleep(5)
            raise RuntimeError("БД недоступна больше минуты")

        def run(self, fn):
            """Выполнить операцию, при обрыве — переподключиться и повторить раз."""
            try:
                return fn(self.conn)
            except psycopg.OperationalError:
                self.reconnect()
                return fn(self.conn)

    db = Db()
    worker = f"waf-browser:{os.getpid()}"
    print("загружаю CLIP…", flush=True)
    clip = Clip()
    print(f"готово. воркер {worker}\n", flush=True)

    rn = next_round_no()
    done = 0
    captchas = 0
    stats = Counter()
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, proxy={"server": args.proxy})
        ctx = br.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        try:
            while not args.limit or done < args.limit:
                aid = db.run(lambda c: claim(c, worker, 60))
                if aid is None:
                    print("очередь пуста", flush=True)
                    break
                base = f"https://www.parkrun.org.uk/parkrunner/{aid}/"
                t0 = time.time()
                try:
                    pg.goto(base, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(3)
                    html = pg.content()
                    if "Human Verification" in html or "Choose all" in html:
                        captchas += 1
                        ok, n, rn = pass_captcha(pg, clip, rn, args.max_puzzles)
                        stats["капча решена" if ok else "капча НЕ решена"] += 1
                        if not ok:
                            db.run(lambda c: (c.execute(
                                "UPDATE crawl_queue SET status='pending', claimed_by=NULL "
                                "WHERE athlete_id=%s", (aid,)), c.commit()))
                            print(f"  атлет {aid}: капчу не прошли — вернул в очередь", flush=True)
                            continue
                        pg.goto(base, wait_until="domcontentloaded", timeout=60000)
                        time.sleep(2)
                        html = pg.content()
                    data = parse_summary(html, str(aid))
                    if data.status == "ok":
                        pg.goto(base + "all/", wait_until="domcontentloaded", timeout=60000)
                        time.sleep(2)
                        h2 = pg.content()
                        if "Choose all" not in h2 and "Human Verification" not in h2:
                            data.runs = parse_all_runs(h2, str(aid))
                    raw = html if data.status == "unclassified" else None
                    db.run(lambda c: (store(c, aid, data, raw), c.execute(
                        "UPDATE crawl_queue SET status=%s, claimed_by=NULL, fetched_at=now() "
                        "WHERE athlete_id=%s", (data.status, aid)), c.commit()))
                    done += 1
                    stats[data.status] += 1
                    print(f"  #{done} атлет {aid}: {data.name or data.status} "
                          f"({data.status}, {data.total_runs or 0} заб.) "
                          f"[{time.time()-t0:.1f}с]", flush=True)
                except Exception as exc:
                    try:
                        db.conn.rollback()
                    except Exception:
                        pass
                    err = repr(exc)[:200]
                    db.run(lambda c: (c.execute(
                        "UPDATE crawl_queue SET status='pending', claimed_by=NULL, "
                        "attempts=attempts+1, error=%s WHERE athlete_id=%s", (err, aid)), c.commit()))
                    print(f"  атлет {aid}: сбой {exc!r}", flush=True)
                    stats["сбой"] += 1
                time.sleep(args.delay)
        except KeyboardInterrupt:
            print("\nостановлено.", flush=True)
        finally:
            # Раздельно, не одной строкой — та же причина, что в harvest_token():
            # если ctx.close() кинет исключение, br.close() не выполнится вовсе.
            try:
                ctx.close()
            except Exception as exc:
                print(f"  контекст браузера не закрылся: {exc!r}", flush=True)
            try:
                br.close()
            except Exception as exc:
                print(f"  !! браузер НЕ закрылся, процесс мог остаться висеть: {exc!r}", flush=True)
            try:
                db.conn.close()
            except Exception:
                pass

    lib = len(os.listdir(f"{DATA}/library")) if os.path.isdir(f"{DATA}/library") else 0
    print("\n" + "=" * 46)
    print(f"АТЛЕТОВ записано в БД: {done}")
    print(f"капч встретилось: {captchas}")
    print("разбивка:", dict(stats))
    print(f"библиотека картинок: {lib}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Свой решатель капчи AWS WAF")
    ap.add_argument("proxy", nargs="?", default="",
                    help="(устар.) позиционный прокси; используй --proxy / --proxy-file")
    ap.add_argument("--proxy", dest="proxy_opt", default="",
                    help="внешний прокси для запросов, напр. https://host:8444 "
                         "или https://user:pass@host:port")
    ap.add_argument("--proxy-file", default="",
                    help="файл со списком прокси (по одному в строке); при запуске "
                         "спросит, каким пользоваться")
    ap.add_argument("--proxy-index", type=int, default=0,
                    help="взять из файла строку N (1-based) без вопроса — для скриптов")
    ap.add_argument("--proxy-scheme", default="http",
                    help="схема для строк без ://; для mcccx-прокси = https (по умолчанию http)")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--max-puzzles", type=int, default=6,
                    help="сколько головоломок подряд решать в одном заходе")
    ap.add_argument("--work", action="store_true",
                    help="РАБОЧИЙ режим: качать реальных атлетов из очереди и писать в БД")
    ap.add_argument("--limit", type=int, default=0,
                    help="сколько атлетов в рабочем режиме (0 = без предела)")
    ap.add_argument("--delay", type=float, default=2.0, help="пауза между атлетами, сек")
    ap.add_argument("--dsn", default="postgresql://parkrun:parkrun_world_local@127.0.0.1:5433/parkrun_world",
                    help="DSN pm-postgres (через SSH-проброс порта 5433)")
    ap.add_argument("--fast", action="store_true",
                    help="БЫСТРЫЙ режим: httpx качает страницы, браузер только на капчу")
    ap.add_argument("--exit-port", type=int, default=0,
                    help="порт выхода xray на сервере (напр. 10859=de2); включает авто-тоннель")
    ap.add_argument("--exit-file", default="",
                    help="файл приватных выходов (строки «имя порт»); при старте спросит, какой")
    args = ap.parse_args()

    # Выбор приватного выхода из файла (путь А: через серверный xray по SSH-тоннелю).
    if not args.exit_port and args.exit_file:
        try:
            rows = []
            for ln in open(args.exit_file, encoding="utf-8"):
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.replace("=", " ").split()
                port = int(parts[-1])
                name = parts[0] if len(parts) > 1 else str(port)
                rows.append((name, port))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"не разобрать файл выходов {args.exit_file}: {exc}")
        if not rows:
            raise SystemExit(f"файл выходов пуст: {args.exit_file}")
        print("Выбери приватный выход:")
        for i, (nm, pt) in enumerate(rows, 1):
            print(f"  {i:>2}) {nm}  (порт {pt})")
        raw = input("номер: ").strip()
        if not raw.isdigit() or not (1 <= int(raw) <= len(rows)):
            raise SystemExit("нужен номер из списка")
        args.exit_port = rows[int(raw) - 1][1]

    # --- выбор внешнего прокси (платные из файла / напрямую) ---
    # ТОЛЬКО из --proxy. Позиционный больше НЕ читаем: старый вызов «... unused --fast»
    # передаёт заглушку unused, и если её принять за прокси — запуск падает на
    # предпроверке (http://unused не отвечает). Позиционный оставлен принимаемым,
    # но игнорируется — для обратной совместимости со старой командой.
    chosen = args.proxy_opt
    if not chosen and args.proxy_file:
        try:
            lines = [ln.strip() for ln in open(args.proxy_file, encoding="utf-8")
                     if ln.strip() and not ln.strip().startswith("#")]
        except OSError as exc:
            raise SystemExit(f"не читается файл прокси {args.proxy_file}: {exc}")
        if not lines:
            raise SystemExit(f"файл прокси пуст: {args.proxy_file}")
        if args.proxy_index:
            if not (1 <= args.proxy_index <= len(lines)):
                raise SystemExit(f"--proxy-index {args.proxy_index} вне 1..{len(lines)}")
            chosen = lines[args.proxy_index - 1]
        else:
            print("Выбери прокси:")
            for i, ln in enumerate(lines, 1):
                print(f"  {i:>2}) {ln}")
            raw = input("номер: ").strip()
            if not raw.isdigit() or not (1 <= int(raw) <= len(lines)):
                raise SystemExit("нужен номер из списка")
            chosen = lines[int(raw) - 1]
    # Отрезаем ИНЛАЙН-комментарий (напр. «socks5://127.0.0.1:10859  # de2»): без
    # этого весь хвост уезжал в адрес прокси, и Chromium падал ERR_PROXY_CONNECTION_FAILED.
    # Из комментария заодно берём имя выхода для табло.
    args.proxy_label = ""
    if chosen and "#" in chosen:
        chosen, _, lbl = chosen.partition("#")
        args.proxy_label = lbl.strip()
    if chosen:
        chosen = chosen.split()[0].strip()  # только сам адрес, без пробелов/хвостов
    if chosen and "://" not in chosen:
        chosen = f"{args.proxy_scheme}://{chosen}"
    args.proxy = chosen  # единое поле, которым дальше пользуется весь код

    tun = None
    # Тоннель нужен всегда (БД на сервере), а вот порт выхода — только если
    # ходим через сервер. Без --exit-port качаем СВОИМ каналом: быстрее, и
    # тоннель тогда всего один — к базе.
    if args.exit_port or args.fast or args.work:
        # Сами поднимаем SSH-тоннель сразу к ДВУМ портам: выход xray и pm-postgres.
        # Так скрипт запускается одной командой, без ручного ssh -L в соседнем окне.
        from sshtunnel import SSHTunnelForwarder

        host = _cred_env("PM_SSH_HOST", "TEMP_SSH_HOST", "PROD_SSH_HOST", default="195.58.34.112")
        user = _cred_env("PM_SSH_USER", "TEMP_SSH_USER", "PROD_SSH_USER", default="viewer")
        pwd = _cred_env("PM_SSH_PASS", "TEMP_SSH_PASSWORD")
        if not pwd:
            from getpass import getpass
            pwd = getpass("SSH-пароль сервера: ")
        # Порты выбираем СВОБОДНЫЕ, но один раз и запоминаем: тоннель может
        # пересоздаваться на ходу, и адреса прокси/БД при этом меняться не должны.
        # Жёстко зашитые номера не годятся — мешают запустить несколько выходов
        # параллельно и ломают старт, если прошлый прогон ещё держит порт.
        def _free_port() -> int:
            import socket
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        lp_proxy, lp_db = _free_port(), _free_port()

        remote = ([("127.0.0.1", args.exit_port), ("127.0.0.1", 5433)] if args.exit_port
                  else [("127.0.0.1", 5433)])
        local = ([("127.0.0.1", lp_proxy), ("127.0.0.1", lp_db)] if args.exit_port
                 else [("127.0.0.1", lp_db)])

        def build():
            return SSHTunnelForwarder(
                (host, 22), ssh_username=user, ssh_password=pwd,
                remote_bind_addresses=remote, local_bind_addresses=local,
                set_keepalive=15.0)

        what = f"выход {args.exit_port} + БД" if args.exit_port else "только БД"
        print(f"поднимаю тоннель к {user}@{host} ({what})…", flush=True)
        tun = build()
        try:
            tun.start()
        except Exception as exc:
            # Голый трейсбэк sshtunnel ничего не объясняет — подсказываем, что смотреть.
            print("\n!! Не удалось подключиться к серверу по SSH.", flush=True)
            print(f"   {type(exc).__name__}: {str(exc)[:160]}\n", flush=True)
            print("   Проверь по порядку:", flush=True)
            print(f"   1) Доступен ли порт: PowerShell → Test-NetConnection {host} -Port 22", flush=True)
            print(f"      (или просто: ssh {user}@{host} — в Windows 10+ ssh встроен)", flush=True)
            print("   2) Пароль. При вводе он не отображается, опечатку не видно.", flush=True)
            print("      Надёжнее задать заранее:  set PM_SSH_PASS=пароль", flush=True)
            print("   3) Не режет ли исходящий 22-й порт сеть/провайдер/антивирус.", flush=True)
            raise SystemExit(1)
        # Только при выходе через сервер прокси = локальный порт тоннеля.
        # Иначе оставляем внешний прокси (--proxy/--proxy-file) или пусто (свой канал).
        if args.exit_port:
            args.proxy = f"http://127.0.0.1:{lp_proxy}"
        args.dsn = f"postgresql://parkrun:parkrun_world_local@127.0.0.1:{lp_db}/parkrun_world"

        def ensure_tunnel() -> bool:
            """Жив ли тоннель; если SSH-сессия умерла — пересоздать целиком.

            sshtunnel сам сессию НЕ восстанавливает: при обрыве он до бесконечности
            пишет «SSH session not active», а прокси и БД лежат. Обрыв ловили на
            браузерном заходе — браузер открывает десятки соединений разом, на
            каждое заводится свой SSH-канал, и это упирается в MaxSessions у sshd.
            """
            nonlocal tun
            try:
                if tun.is_active:
                    return True
            except Exception:
                pass
            print("    тоннель умер — пересоздаю…", flush=True)
            try:
                tun.stop()
            except Exception:
                pass
            for attempt in range(10):
                try:
                    tun = build()
                    tun.start()
                    print("    тоннель поднят заново", flush=True)
                    return True
                except Exception as exc:
                    print(f"    попытка {attempt+1}/10: {exc!r}", flush=True)
                    time.sleep(10)
            return False

        args.ensure_tunnel = ensure_tunnel
        print("тоннель поднят: " + (f"выход → {args.proxy}" if args.proxy
                                    else "запросы идут СВОИМ каналом (без прокси)") + "\n",
              flush=True)

    # Предпроверка внешнего прокси: сразу видно, авторизует ли он ЭТУ машину,
    # а не выясняется через минуту работы. Для платных mcccx с привязкой к IP
    # это главный источник «ничего не работает».
    if args.proxy and "127.0.0.1" not in args.proxy:
        import httpx as _hx
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
        try:
            with _hx.Client(proxy=args.proxy, timeout=15, follow_redirects=True,
                            headers={"User-Agent": ua}) as _c:
                _r = _c.get("https://www.parkrun.org.uk/parkrunner/620/")
            if "(A620)" in _r.text:
                print(f"прокси OK — проходит без капчи ({proxy_label(args.proxy)})\n", flush=True)
            elif _r.status_code in (403, 405) or "Human Verification" in _r.text:
                print(f"прокси OK — даёт капчу, будем решать ({proxy_label(args.proxy)})\n", flush=True)
            else:
                print(f"прокси ответил HTTP {_r.status_code} — необычно, но продолжаю\n", flush=True)
        except Exception as exc:
            if tun is not None:
                try:
                    tun.stop()
                except Exception:
                    pass
            raise SystemExit(
                f"\n!! Прокси не отвечает: {type(exc).__name__}: {str(exc)[:120]}\n"
                "   Вероятнее всего он НЕ авторизует IP этой машины (привязка к другому IP).\n"
                "   Перепривяжи прокси у провайдера на этот IP или запускай с сервера.")

    try:
        if args.fast:
            work_fast(args)
            return
        if args.work:
            work_mode(args)
            return
    finally:
        if tun is not None:
            try:
                tun.stop()
            except Exception:
                pass

    from playwright.sync_api import sync_playwright

    print("загружаю CLIP…", flush=True)
    clip = Clip()
    print("модель готова.\n", flush=True)

    rn = next_round_no()
    stats = Counter()
    with sync_playwright() as p:
        for attempt in range(1, args.rounds + 1):
            br = p.chromium.launch(headless=True, proxy={"server": args.proxy})
            ctx = br.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                viewport={"width": 1280, "height": 900})
            pg = ctx.new_page()
            print(f"[заход {attempt}/{args.rounds}]", flush=True)
            try:
                pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)
                if BARCODE in pg.content():
                    print("    капчи не было — страница отдалась сразу", flush=True)
                    stats["без капчи"] += 1
                    ctx.close(); br.close(); continue
                for sel in ["button:has-text('Begin')", "text=Begin"]:
                    try:
                        el = pg.locator(sel).first
                        if el.count() and el.is_visible():
                            el.click(timeout=5000); break
                    except Exception:
                        continue
                time.sleep(6)
                solved = False
                for k in range(args.max_puzzles):
                    r = solve_once(pg, clip, rn); rn += 1
                    if r.get("error"):
                        print(f"    сбой: {r['error']}", flush=True)
                        break
                    if r.get("solved"):
                        solved = True
                        print(f"    ✅ ПРОЙДЕНО с {k+1}-й головоломки", flush=True)
                        break
                    if r.get("another_puzzle"):
                        print("    → следующая головоломка", flush=True)
                        time.sleep(3)
                        continue
                    break
                stats["решено" if solved else "не решено"] += 1
            except Exception as exc:
                print(f"    ошибка: {exc!r}", flush=True)
                stats["ошибка"] += 1
            ctx.close(); br.close()

    lib = len(os.listdir(f"{DATA}/library")) if os.path.isdir(f"{DATA}/library") else 0
    print("\n" + "=" * 46)
    print("ИТОГ:", dict(stats))
    ok = stats["решено"]; tried = ok + stats["не решено"]
    if tried:
        print(f"успешность решателя: {ok}/{tried} = {100*ok/tried:.0f}%")
    print(f"библиотека картинок: {lib} уникальных")
    json.dump({"library": lib, "stats": dict(stats)},
              open(f"{DATA}/stats.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
