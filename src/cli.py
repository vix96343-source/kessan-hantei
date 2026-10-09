"""エントリポイント。GitHub Actions も手動実行も同じ関数を呼ぶ。"""
import argparse
import json
import logging
import sys
from datetime import date

from . import calendar_fetch, disclosures, earnings, reaction, site, universe
from .bizdays import add_business_days
from .config import load_config, now_jst


def cmd_disclosures(args, cfg):
    """15分おき: TDnet取り込み → 新しい決算短信の型判定 → (変化があれば)サイト再生成"""
    stats = disclosures.ingest(cfg, days=args.days)
    stats["earnings"] = earnings.update(cfg)
    # 変化が無い回はページも作り直さない(生成時刻だけ変わる無駄なコミットを防ぐ)
    if stats["new_rows"] or stats["xbrl_parsed"] or stats["earnings"]["evaluated"]             or not (site.DOCS_DIR / "index.html").exists():
        stats["pages"] = site.render_all(cfg)
    print(json.dumps(stats, ensure_ascii=False))


def cmd_earnings(args, cfg):
    """未評価の決算短信を型判定し、上昇確度の集計とサイトを更新(手動・バックフィル用)"""
    print(json.dumps(earnings.update(cfg, limit=args.limit), ensure_ascii=False))
    reaction.restat(cfg)
    print(site.render_all(cfg))


def prefetch_codes(cfg) -> list[str]:
    """翌営業日〜prefetch_business_days 先に発表予定で、一次フィルタ(売買代金)を通る銘柄"""
    cal, uni = calendar_fetch.load(), universe.load()
    until = add_business_days(now_jst().date(), cfg["earnings"]["prefetch_business_days"]).isoformat()
    liquid = set(uni.loc[uni["avg_turnover_20d"] >= cfg["judge"]["min_avg_turnover"], "code"])
    return sorted(set(cal.loc[cal["announce_date"] <= until, "code"]) & liquid)


def cmd_universe(args, cfg):
    print(json.dumps(universe.update(cfg), ensure_ascii=False))


def cmd_calendar(args, cfg):
    print(json.dumps(calendar_fetch.update(cfg), ensure_ascii=False, default=str))


def cmd_run(args, cfg):
    """dailyワークフロー: (週1) universe → calendar → 発表予定銘柄の履歴を先取り → 株価反応 → サイト再生成"""
    today = now_jst().date()
    if args.force_universe or not universe.path().exists() or today.weekday() == cfg["universe"]["update_weekday"]:
        cmd_universe(args, cfg)
    cmd_calendar(args, cfg)
    codes = prefetch_codes(cfg)
    print(json.dumps({"prefetched": earnings.prefetch_history(cfg, codes), "targets": len(codes)}))
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

    s = sub.add_parser("run", help="universe(週1) → calendar → 履歴先取り → 株価反応 → サイト再生成")
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
