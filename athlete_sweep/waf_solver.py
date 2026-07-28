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
        import open_clip
        import torch

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
    time.sleep(5)
    body = pg.content()
    # Успех = капчи на странице БОЛЬШЕ НЕТ. Раньше проверяли по штрихкоду (A620),
    # но в рабочем режиме качаются другие атлеты — у них штрихкод свой, и условие
    # не выполнялось никогда: решённые капчи считались проваленными.
    info["another_puzzle"] = "Choose all" in body
    info["solved"] = not info["another_puzzle"] and "Human Verification" not in body
    return info


def pass_captcha(pg, clip: Clip, rn: int, max_puzzles: int = 4) -> tuple[bool, int, int]:
    """Провести страницу через капчу. Возвращает (прошли, решено_головоломок, rn)."""
    for sel in ["button:has-text('Begin')", "text=Begin"]:
        try:
            el = pg.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=5000)
                break
        except Exception:
            continue
    time.sleep(6)
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
            while done < args.limit:
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
            ctx.close(); br.close()
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
    ap.add_argument("proxy", help="прокси выхода, напр. http://127.0.0.1:10859")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--max-puzzles", type=int, default=4,
                    help="сколько головоломок подряд решать в одном заходе")
    ap.add_argument("--work", action="store_true",
                    help="РАБОЧИЙ режим: качать реальных атлетов из очереди и писать в БД")
    ap.add_argument("--limit", type=int, default=100, help="сколько атлетов в рабочем режиме")
    ap.add_argument("--delay", type=float, default=2.0, help="пауза между атлетами, сек")
    ap.add_argument("--dsn", default="postgresql://parkrun:parkrun_world_local@127.0.0.1:5433/parkrun_world",
                    help="DSN pm-postgres (через SSH-проброс порта 5433)")
    args = ap.parse_args()
    if args.work:
        work_mode(args)
        return

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
