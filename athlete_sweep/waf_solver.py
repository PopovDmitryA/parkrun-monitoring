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
    info["solved"] = BARCODE in body
    info["another_puzzle"] = "Choose all" in body
    return info


def main() -> None:
    ap = argparse.ArgumentParser(description="Свой решатель капчи AWS WAF")
    ap.add_argument("proxy", help="прокси выхода, напр. http://127.0.0.1:10859")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--max-puzzles", type=int, default=4,
                    help="сколько головоломок подряд решать в одном заходе")
    args = ap.parse_args()

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
