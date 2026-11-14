"""エントリポイント。GitHub Actions も手動実行も同じ関数を呼ぶ。"""
import argparse
import json
import logging
import sys
from datetime import date, timedelta

from . import backtest, breakdown, fy_months, calendar_fetch, consensus, disclosures, earnings, forecasts, irdocs, judge, order_adjust, orders, pres_orders, pres_seg_orders, pres_tables, reaction, revisions, segments, site, universe
from .config import DATA_DIR, load_config, now_jst


BACKLOG = DATA_DIR / "cache" / "earnings_backlog.txt"   # 数値をまだ読んでいない短信の数(watch.yml が見る)
# 監視の後半(details・irdocs)は別プロセスで並行して回す(watch.yml)。後半はサイトを作らずこの印を置き、
# サイトは前半(disclosures --fast)が次の回(15秒以内)に作る(2つのプロセスが同時に docs/ を書かないように)
RENDER_FLAG = DATA_DIR / "cache" / "render_needed"


def _request_render(defer: bool, cfg) -> list[str]:
    if defer:
        RENDER_FLAG.parent.mkdir(parents=True, exist_ok=True)
        RENDER_FLAG.write_text("1", encoding="utf-8")
        return []
    return site.render_all(cfg)


def _atomic_csv():
    """CSV を一時ファイルに書いてから置き換える(並行する監視の前半・後半が書きかけの CSV を読まないように)"""
    import os
    from pathlib import Path
    import pandas as pd
    orig = pd.DataFrame.to_csv
    if getattr(orig, "_atomic", False):
        return

    def to_csv(self, path_or_buf=None, *a, **k):
        if isinstance(path_or_buf, (str, Path)) and str(path_or_buf).endswith(".csv"):
            dst = Path(path_or_buf)
            tmp = dst.with_name(f"{dst.name}.{os.getpid()}.partial")
            r = orig(self, tmp, *a, **k)
            os.replace(tmp, dst)
            return r
        return orig(self, path_or_buf, *a, **k)
    to_csv._atomic = True
    pd.DataFrame.to_csv = to_csv
FAST_LIMIT = 20     # --fast の1回に読む短信の数(1秒/件なので約20秒ごとに公開できる)


def cmd_disclosures(args, cfg):
    """監視の1回: TDnet取り込み → 短信・修正の数値 → (変化があれば)サイト再生成。
    --fast は数値だけ(短信は FAST_LIMIT 件ずつ、すぐ公開するため)。受注・セグメント等は cmd_details"""
    stats = disclosures.ingest(cfg, days=args.days)
    stats["earnings"] = earnings.update(cfg, limit=FAST_LIMIT if args.fast else None)
    stats["revisions"] = revisions.update(cfg)    # 修正の行に出す修正後の予想など
    BACKLOG.parent.mkdir(parents=True, exist_ok=True)
    BACKLOG.write_text(str(stats["earnings"]["remaining"]), encoding="utf-8")
    changed = stats["new_rows"] or stats["xbrl_parsed"] or stats["earnings"]["evaluated"] or stats["revisions"]
    if not args.fast:
        changed = _details(cfg, stats) or changed
    # 変化が無い回はページも作り直さない(生成時刻だけ変わる無駄なコミットを防ぐ)
    # (IRサイトの説明資料探しは固まっても監視を止めないよう、watch.yml で別プロセスとして実行する)
    if RENDER_FLAG.exists():                      # 並行する後半(受注・セグメント・IR探し)が変えた分
        RENDER_FLAG.unlink(missing_ok=True)
        changed = True
    if changed or not (site.DOCS_DIR / "index.html").exists() or manual_changed():
        stats["pages"] = site.render_all(cfg)
    print(json.dumps(stats, ensure_ascii=False))


def _details(cfg, stats) -> bool:
    # 1つの処理が落ちても残りは必ず回す(2026-11-10 に受注の補正が落ちてセグメントが1日止まった)
    def step(fn, name):
        try:
            return fn(cfg) or 0
        except Exception:
            logging.getLogger(__name__).exception("%s が失敗(この回は飛ばす)", name)
            return 0
    stats["fy_months"] = step(fy_months.update, "決算月")    # 四半期業績の表の「27/3 2Q」用(短信の無い会社の決算月)
    stats["orders"] = step(orders.update, "受注")             # 短信の受注高・受注残高の表
    stats["orders"] += step(pres_orders.update, "説明資料の受注")  # 短信に受注が無い会社は説明資料の表から
    stats["orders"] += step(pres_tables.update, "説明資料の推移表")  # 説明資料の四半期推移の表の品目別の売上・受注
    stats["orders"] += step(pres_seg_orders.update, "説明資料のセグメント別受注")  # 短信に合計しか無い受注に説明資料のセグメント
    stats["order_adjust"] = step(order_adjust.update, "受注の補正")  # 受注の3か月の値の1の誤差を説明資料で直す
    stats["segments"] = step(segments.update, "セグメント")    # 短信のセグメント別の売上・利益
    stats["segments"] += step(breakdown.update, "内訳")       # 品目別・製品別などの売上の内訳(セグメントが1つの会社など)
    return bool(stats["orders"] or stats["segments"] or stats["order_adjust"])


def cmd_details(args, cfg):
    """監視の1回の後半: 受注・セグメント・品目別など(短信の zip は数値を読んだときのものを使う)"""
    stats = {}
    if _details(cfg, stats):
        stats["pages"] = _request_render(args.defer_render, cfg)
    print(json.dumps(stats, ensure_ascii=False))


def cmd_earnings(args, cfg):
    """未評価の決算短信を型判定し、上昇確度の集計とサイトを更新(手動・バックフィル用)"""
    print(json.dumps(earnings.update(cfg, limit=args.limit), ensure_ascii=False))
    reaction.restat(cfg)
    print(site.render_all(cfg))


def manual_changed() -> bool:
    """手で登録した説明資料(data/manual_docs.csv)が、前回サイトを作ったときから変わったか"""
    p = site.DOCS_DIR / "data" / "index.json"
    if not p.exists():
        return True
    from . import good_rules
    idx = json.loads(p.read_text(encoding="utf-8"))
    # 手で登録した説明資料か、好決算の基準(good_rules.yaml)が変わったら作り直す
    return idx.get("manual", "") != irdocs.manual_hash() or idx.get("good_rules", "") != good_rules.file_hash()


def cmd_irdocs(args, cfg):
    """TDnet に無い説明資料を各社IRサイトで探し、見つかればサイトを作り直す"""
    stats = irdocs.update(cfg, time_budget_sec=args.budget)
    if stats["found"]:
        stats["pages"] = _request_render(args.defer_render, cfg)
    print(json.dumps(stats, ensure_ascii=False))


def cmd_orders(args, cfg):
    """決算短信の受注高・受注残高の表を読み、サイトを作り直す(手動・やり直し用)"""
    n = orders.update(cfg, limit=args.limit)
    # 前の四半期の記録が無い会社は、IRBANK が保管している前の期の短信(PDF)で補う(手元でのみ。Actions からは IRBANK が使えない)
    b = orders.backfill(cfg) if args.backfill else {}
    print(json.dumps({"orders": n, "backfill": b, "pages": site.render_all(cfg) if n or b.get("ok") else []},
                     ensure_ascii=False))


def cmd_consensus(args, cfg):
    """アナリスト予想を取る(既定: 決算予定の銘柄。--recent-days で最近決算を出した銘柄も)"""
    codes = None
    if args.recent_days:
        d = disclosures.load()
        since = (now_jst() - timedelta(days=args.recent_days)).strftime("%Y-%m-%d")
        codes = sorted(set(d.loc[(d["kind"] == "earnings_report") & (d["disclosed_at"] >= since), "code"]))
    n = consensus.update(cfg, codes=codes)
    print(json.dumps({"consensus": n, "pages": site.render_all(cfg) if n else []}, ensure_ascii=False))


def cmd_universe(args, cfg):
    print(json.dumps(universe.update(cfg), ensure_ascii=False))


def cmd_calendar(args, cfg):
    print(json.dumps(calendar_fetch.update(cfg), ensure_ascii=False, default=str))


def cmd_run(args, cfg):
    """dailyワークフロー: (週1) universe → calendar → 株価反応 → サイト再生成"""
    today = now_jst().date()
    if args.force_universe or not universe.path().exists() or today.weekday() == cfg["universe"]["update_weekday"]:
        cmd_universe(args, cfg)
    cmd_calendar(args, cfg)
    # 決算予定の銘柄のアナリスト予想(発表前の値を残す)
    print(json.dumps({"consensus": consensus.update(cfg)}, ensure_ascii=False))
    print(json.dumps(reaction.update(cfg), ensure_ascii=False))
    try:                                          # 判定の失敗で速報サイトの更新を止めない
        print(f"judge: {len(judge.judge_upcoming(cfg))} 件")   # 決算予定の、まだ発表されていない日すべて
    except Exception as e:
        logging.getLogger(__name__).warning("judge 失敗: %s", e)
    cmd_site(args, cfg)


def cmd_judge(args, cfg):
    """M3: 決算持ち越しの判定(既定: 翌営業日の発表銘柄)。--backfill は手元で IRBANK から前回の短信を補う"""
    if args.backfill:
        t = judge.backfill_targets(cfg)
        if args.code:
            t = [(c, d) for c, d in t if c == args.code] or                 ([(args.code, date.fromisoformat(args.date))] if args.date else [])
        print(json.dumps(forecasts.backfill(cfg, t, judge.have_recent_snapshot(t)), ensure_ascii=False))
        return
    if args.date or args.code:
        ann = date.fromisoformat(args.date) if args.date else None
        out = judge.judge(cfg, announce=ann, code=args.code, write=not args.dry_run)
    else:                                         # 既定: 決算予定の、まだ発表されていない日すべて
        out = judge.judge_upcoming(cfg, write=not args.dry_run)
    if not args.dry_run:
        site.render_all(cfg)
    for r in out.itertuples():
        print(f"{r.announce_date} {r.gap_class:8} {float(r.score_total):+.3f} EV{float(r.ev_pred_pct):+.1f}% {r.code} {r.name} "
              f"[{r.conviction}] {r.reason_quant}")


def cmd_judge_backtest(args, cfg):
    """判定の事後検証: 期間内に決算を出した銘柄を発表前の情報だけで判定し、寄付ギャップと比べる"""
    until = args.until or now_jst().strftime("%Y-%m-%d")
    tg = backtest.targets(cfg, args.since, until)
    if args.backfill:                             # 手元のみ: 発表前の会社予想を IRBANK から補う
        print(json.dumps(backtest.backfill(cfg, tg), ensure_ascii=False))
    df = backtest.run(cfg, tg)
    print(json.dumps(backtest.summary(df, cfg), ensure_ascii=False, indent=1))
    if not args.no_site:
        print(site.render_all(cfg))


def cmd_site(args, cfg):
    print(site.render_all(cfg))


def cmd_revisions(args, cfg):
    """銘柄の revision_recency を確認する(手動リサーチ用)。"""
    asof = date.fromisoformat(args.date) if args.date else now_jst().date()
    df = disclosures.load()
    meta = disclosures.load_meta()
    res = disclosures.revision_recency(args.code, asof, df, cfg["factors"]["revision_recency"],
                                       meta.get("coverage_start"))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    recent = df[(df["code"] == args.code)].head(10)
    for r in recent.itertuples():
        pct = f" {r.change_pct}%" if r.change_pct else ""
        print(f"{r.disclosed_at}  [{r.kind}] {r.direction}{pct}  {r.title}")


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m src.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("disclosures", help="TDnetから適時開示(修正・短信・説明資料)を取り込む")
    s.add_argument("--days", type=int, help="当日から遡る日数(初回バックフィルは 31)")
    s.add_argument("--fast", action="store_true", help="数値だけ(短信は20件ずつ)。受注・セグメント等は details")
    s.set_defaults(func=cmd_disclosures)

    s = sub.add_parser("details", help="受注・セグメント・品目別など(監視の後半)")
    s.add_argument("--defer-render", action="store_true", help="サイトは作らず、監視の前半に作らせる(並行実行用)")
    s.set_defaults(func=cmd_details)

    s = sub.add_parser("universe", help="M1: プライム・スタンダード銘柄一覧と平均売買代金を更新")
    s.set_defaults(func=cmd_universe)

    s = sub.add_parser("calendar", help="M2: 翌営業日〜N営業日先の決算発表予定を更新")
    s.set_defaults(func=cmd_calendar)

    s = sub.add_parser("earnings", help="決算短信の型判定(①リクルート ②キオクシア ③ローツェB)")
    s.add_argument("--limit", type=int, help="評価する短信の上限")
    s.set_defaults(func=cmd_earnings)

    s = sub.add_parser("irdocs", help="TDnet に無い決算説明資料を各社IRサイトで探す")
    s.add_argument("--budget", type=float, default=600, help="使う時間の上限(秒)")
    s.add_argument("--defer-render", action="store_true", help="サイトは作らず、監視の前半に作らせる(並行実行用)")
    s.set_defaults(func=cmd_irdocs)

    s = sub.add_parser("consensus", help="アナリスト予想の平均(yfinance)を取る")
    s.add_argument("--recent-days", type=int, help="この日数以内に決算を出した銘柄も取る")
    s.set_defaults(func=cmd_consensus)

    s = sub.add_parser("orders", help="決算短信の受注高・受注残高の表を読む")
    s.add_argument("--limit", type=int, default=1000)
    s.add_argument("--backfill", action="store_true", help="前の四半期の短信を IRBANK の PDF で補う(QoQ 用・手元で実行)")
    s.set_defaults(func=cmd_orders)

    s = sub.add_parser("run", help="universe(週1) → calendar → 株価反応 → サイト再生成")
    s.add_argument("--force-universe", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("judge", help="M3: 決算持ち越しの判定 → data/predictions.csv")
    s.add_argument("--date", help="発表日 YYYY-MM-DD(既定: 決算予定の、まだ発表されていない日すべて)")
    s.add_argument("--code", help="1銘柄だけ判定(カレンダーに無い過去日も可)")
    s.add_argument("--dry-run", action="store_true", help="predictions.csv に書かない")
    s.add_argument("--backfill", action="store_true", help="手元のみ: 前回の短信を IRBANK から補う")
    s.set_defaults(func=cmd_judge)

    s = sub.add_parser("judge-backtest", help="判定の事後検証 → data/judge_backtest.csv(サイトの「判定」ページ)")
    s.add_argument("--since", required=True, help="この日以降に決算を出した銘柄 YYYY-MM-DD")
    s.add_argument("--until", help="この日まで(既定: 今日)")
    s.add_argument("--backfill", action="store_true", help="手元のみ: 発表前の会社予想を IRBANK から補う")
    s.add_argument("--no-site", action="store_true")
    s.set_defaults(func=cmd_judge_backtest)

    s = sub.add_parser("site", help="docs/ のサイトを再生成(取得なし)")
    s.set_defaults(func=cmd_site)

    s = sub.add_parser("revisions", help="銘柄の直近修正開示と revision_recency を表示")
    s.add_argument("--code", required=True)
    s.add_argument("--date", help="基準日 YYYY-MM-DD(既定: 今日)")
    s.set_defaults(func=cmd_revisions)

    args = p.parse_args(argv)
    _atomic_csv()
    args.func(args, load_config())


if __name__ == "__main__":
    main()
