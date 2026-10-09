"""エントリポイント。GitHub Actions も手動実行も同じ関数を呼ぶ。"""
import argparse
import json
import logging
import sys
from datetime import date

from . import calendar_fetch, disclosures, earnings, irdocs, orders, reaction, revisions, site, universe
from .config import load_config, now_jst


def cmd_disclosures(args, cfg):
    """15分おき: TDnet取り込み → 新しい決算短信の型判定 → (変化があれば)サイト再生成"""
    stats = disclosures.ingest(cfg, days=args.days)
    stats["earnings"] = earnings.update(cfg)
    stats["revisions"] = revisions.update(cfg)    # 修正の行に出す修正後の予想など
    stats["orders"] = orders.update(cfg)          # 短信の受注高・受注残高の表
    # 変化が無い回はページも作り直さない(生成時刻だけ変わる無駄なコミットを防ぐ)
    # (IRサイトの説明資料探しは固まっても監視を止めないよう、watch.yml で別プロセスとして実行する)
    if stats["new_rows"] or stats["xbrl_parsed"] or stats["earnings"]["evaluated"] or stats["revisions"] or stats["orders"] \
            or not (site.DOCS_DIR / "index.html").exists() or manual_changed():
        stats["pages"] = site.render_all(cfg)
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
    return json.loads(p.read_text(encoding="utf-8")).get("manual", "") != irdocs.manual_hash()


def cmd_irdocs(args, cfg):
    """TDnet に無い説明資料を各社IRサイトで探し、見つかればサイトを作り直す"""
    stats = irdocs.update(cfg, time_budget_sec=args.budget)
    if stats["found"]:
        stats["pages"] = site.render_all(cfg)
    print(json.dumps(stats, ensure_ascii=False))


def cmd_orders(args, cfg):
    """決算短信の受注高・受注残高の表を読み、サイトを作り直す(手動・やり直し用)"""
    n = orders.update(cfg, limit=args.limit)
    print(json.dumps({"orders": n, "pages": site.render_all(cfg) if n else []}, ensure_ascii=False))


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
    print(json.dumps(reaction.update(cfg), ensure_ascii=False))
    cmd_site(args, cfg)


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
    s.set_defaults(func=cmd_disclosures)

    s = sub.add_parser("universe", help="M1: プライム・スタンダード銘柄一覧と平均売買代金を更新")
    s.set_defaults(func=cmd_universe)

    s = sub.add_parser("calendar", help="M2: 翌営業日〜N営業日先の決算発表予定を更新")
    s.set_defaults(func=cmd_calendar)

    s = sub.add_parser("earnings", help="決算短信の型判定(①リクルート ②キオクシア ③ローツェB)")
    s.add_argument("--limit", type=int, help="評価する短信の上限")
    s.set_defaults(func=cmd_earnings)

    s = sub.add_parser("irdocs", help="TDnet に無い決算説明資料を各社IRサイトで探す")
    s.add_argument("--budget", type=float, default=600, help="使う時間の上限(秒)")
    s.set_defaults(func=cmd_irdocs)

    s = sub.add_parser("orders", help="決算短信の受注高・受注残高の表を読む")
    s.add_argument("--limit", type=int, default=1000)
    s.set_defaults(func=cmd_orders)

    s = sub.add_parser("run", help="universe(週1) → calendar → 株価反応 → サイト再生成")
    s.add_argument("--force-universe", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("site", help="docs/ のサイトを再生成(取得なし)")
    s.set_defaults(func=cmd_site)

    s = sub.add_parser("revisions", help="銘柄の直近修正開示と revision_recency を表示")
    s.add_argument("--code", required=True)
    s.add_argument("--date", help="基準日 YYYY-MM-DD(既定: 今日)")
    s.set_defaults(func=cmd_revisions)

    args = p.parse_args(argv)
    args.func(args, load_config())


if __name__ == "__main__":
    main()
