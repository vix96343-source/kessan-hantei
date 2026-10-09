# 決算持ち越し判定君 — 要件定義 / CLAUDE.md

このリポジトリは「決算持ち越し判定君」を実装する。翌営業日に決算発表を控えたプライム・スタンダード銘柄を自動抽出し、決算跨ぎ(持ち越し)の期待値を判定してレポートを出力し、発表後に答え合わせを行って判定精度を継続的に改善する、個人用のシステムトレード支援ツールである。

利用者は1名。サーバー不要。CLI + GitHub Actions + GitHub Pages(静的HTML)で完結させる。UIよりデータパイプラインと判定ロジックの品質を最優先すること。

---

## 0. 設計原則

1. **予想と実測を同じ行に持つ。** predictions.csv は予想時に書く列と答え合わせ時に埋める列を最初から両方持つ。これがこのツールの核心。勝率・EVは運用が進むにつれてLLMの感覚値から実測値に置き換わる。
2. **自動と手動でコードを分けない。** GitHub Actions も手動CLIも同じ関数を呼ぶだけにする。
3. **全履歴をgitに残す。** predictions.csv、rules.md、config.yaml の変更履歴がそのまま検証記録になる。
4. **二段構え。** 決算ピーク日(8月頭・11月頭・2月頭・4月末)は1日500〜900社発表される。軽い一次フィルタで数十件に絞ってから詳細判定する。全件詳細判定は禁止。
5. **判定ロジックは config.yaml で調整可能にする。** 重み・閾値のハードコード禁止。

---

## 1. アーキテクチャ

```
[JPX上場銘柄一覧] ─┐
[J-Quants API] ────┼→ M1 universe → universe.csv
                   │
[J-Quants /fins/announcement] ─┐
[株探 決算スケジュール(補完)] ──┼→ M2 calendar → calendar.csv
                               │
universe.csv × calendar.csv → 翌営業日発表×プライム/スタンダード銘柄
        ↓
M3 judge(一次フィルタ → 詳細スコアリング) → predictions.csv 追記
        ↓
M4 report(Jinja2でHTML生成) → docs/ → GitHub Pages公開
        ↓ (発表翌営業日の夜)
M5 review(実績取込・答え合わせ) → predictions.csv 更新 + stats.json 再計算 → レポート再生成
```

### データソース(決定事項)

| 用途 | 一次ソース | 補完/代替 |
|---|---|---|
| 上場銘柄・市場区分 | **JPX公式 上場銘柄一覧 data_j.xlsx(月次)**(実装済み) | J-Quants `/listed/info` |
| 決算発表予定日 | **JPX公式 決算発表予定日 kessanMM_MMDD.xlsx**(実装済み。各社が取引所に届け出た日付) | J-Quants `/fins/announcement` / 株探(発表時刻) |
| 平均売買代金 | yfinance(終値×出来高の近似) | J-Quants `/prices/daily_quotes` |
| 財務諸表・会社予想・進捗率計算 | J-Quants `/fins/statements` | 決算短信XBRL(TDnet) |
| 修正開示・短信・説明資料(適時開示) | TDnet 適時開示情報閲覧サービス(一覧+業績予想修正XBRL) | J-Quants `/fins/statements`(過去分の修正履歴) |
| 株価(判定用の履歴) | J-Quants `/prices/daily_quotes` | yfinance |
| 株価(答え合わせの寄付・終値) | yfinance(当日データ) | J-Quants(翌日反映) |
| コンセンサス | **v1では使わない**(下記「設計判断メモ」参照) | Phase 2でプラガブルに追加 |

- **2026-10時点: J-Quants未契約。M1/M2/開示はJPX・TDnetの無料公式データで実装済み。** J-Quantsは財務諸表(進捗率計算)で必要になった時点で契約を再検討する。J-Quants由来データは規約上の再配布にあたるため、公開リポジトリにはコミットしない(Actions内でのみ使用)。
- J-Quantsは**Lightプラン(月額約1,650円)を契約する前提**で設計する。無料プランは12週遅延があり実運用に使えない。認証情報はGitHub Actions Secretsと`.env`(gitignore)で管理。
- **実装開始時に必ずJ-Quants API仕様を公式ドキュメントで確認すること。** 特に `/fins/announcement` のカバレッジ(決算期による制限の有無)。カバレッジに穴があれば株探スクレイピングを一次ソースに昇格させる。
- スクレイピングは1リクエスト/秒以下にレート制限し、User-Agent明示、失敗時リトライ3回。

---

## 2. ディレクトリ構成

```
/
├── CLAUDE.md              # 本ファイル
├── config.yaml            # 重み・閾値・フィルタ条件
├── rules.md               # 判定ルールと教訓(答え合わせから蓄積)
├── src/
│   ├── universe.py        # M1
│   ├── calendar_fetch.py  # M2
│   ├── judge.py           # M3
│   ├── report.py          # M4
│   ├── review.py          # M5
│   ├── datasources/       # jquants.py / kabutan.py / yf.py(プラガブル)
│   └── cli.py             # エントリポイント(全モジュール共通)
├── data/
│   ├── universe.csv
│   ├── calendar.csv
│   ├── predictions.csv    # ★ コア資産
│   ├── stats.json         # GAP判定クラス別の実測成績
│   ├── disclosures.csv    # 適時開示(修正・短信・説明資料)。disclosure_idキーで追記
│   ├── tdnet_earnings.csv  # 決算短信XBRLの累計値(②の前四半期比に使う。自前で蓄積)
│   ├── earnings_features.csv  # 決算短信ごとの型判定(①②③)
│   ├── reactions.csv / reaction_stats.json  # 開示後の株価反応と上昇確度の集計
│   └── disclosures_meta.json  # coverage_start(欠損なし期間の開始日)/ last_date
├── docs/                  # GitHub Pages出力先(生成物)
├── templates/             # Jinja2テンプレート
└── .github/workflows/
    ├── daily.yml          # 平日20:05 JST(cron: '5 11 * * 1-5')+ workflow_dispatch
    ├── review.yml         # 平日19:30 JST + workflow_dispatch
    └── watch.yml          # 平日 JST 8:00〜20:00 に1分おき(TDnet 監視・リアルタイム表示)
```

CLIコマンド体系:
```
python -m src.cli universe            # universe.csv更新
python -m src.cli calendar            # 決算カレンダー更新
python -m src.cli judge [--date YYYY-MM-DD] [--code 4617]  # 判定(日付指定 or 単一銘柄)
python -m src.cli report              # HTML再生成
python -m src.cli review [--date YYYY-MM-DD]               # 答え合わせ
python -m src.cli run                 # calendar→judge→report を一括(dailyワークフローが呼ぶ)
python -m src.cli disclosures [--days N]   # TDnet適時開示の取り込み(初回は --days 31)
python -m src.cli revisions --code 4617 [--date YYYY-MM-DD]  # 直近修正と revision_recency を確認
```

`--code` 単体判定は手動リサーチ用(例: 中国塗料だけ今すぐ判定したい)。

---

## 3. モジュール仕様

### M1: universe.py
- JPX上場銘柄一覧からプライム・スタンダードの内国株式のみ抽出(グロース・外国株式除外)。約3,100銘柄。
- 直近20日平均売買代金を計算して列に持つ(一次フィルタで使用)。
- 出力: `universe.csv` (code, name, market, sector33, avg_turnover_20d, updated_at)
- 月次更新で十分だが、dailyワークフロー内で週1(月曜)に自動更新。

### M2: calendar_fetch.py
- JPX決算発表予定日から翌営業日〜5営業日先の発表予定を取得し `calendar.csv` を毎日洗い替え(発表日の延期・変更は普通に起きるため差分ではなく全置換)。
- 株探から発表時刻を取得し、15:00より前なら `announce_timing=場中`、以降なら `引け後`。取得不能時は `引け後` をデフォルトとし `time_confirmed=false` フラグ。 ※2026-10: 株探は GitHub Actions のIPから 405 で拒否されるため、Actions 上では使えない。
- **場中発表銘柄はレポートで明確に警告表示する**(前日中に仕込む必要があるため)。
- 出力: `calendar.csv` (announce_date, code, announce_time, announce_timing, fiscal_q, source, updated_at)

### M3: judge.py

**一次フィルタ(軽い・全件対象):**
- 市場区分 ∈ {プライム, スタンダード}
- 平均売買代金 ≥ config指定値(初期値: 1億円/日)
- 通過銘柄が30を超える場合、簡易プレスコア上位30件に制限(config可変)

**詳細スコアリング(一次フィルタ通過銘柄のみ):**

スコアは以下の因子の加重和。重みは config.yaml で定義。

| 因子 | 内容 | 初期重み |
|---|---|---|
| progress_gap | 今期の進捗率 −過去3年同Q平均進捗率(最重要シグナル) | 0.35 |
| guidance_conservatism | 過去3年の期中上方修正回数・幅(保守的ガイダンス癖の定量化) | 0.25 |
| beat_history | 過去8四半期のQ実績が四半期換算計画を上回った回数 | 0.15 |
| momentum_pricing | 直近1ヶ月リターン・25日線乖離。**過度な事前上昇は織り込みリスクとして減点**(逆張り側の発想: 期待が乗り切った状態での好決算は反応が鈍い) | 0.15(負方向) |
| revision_recency | 直近90日以内に修正開示済みか(直前修正済みならサプライズ余地減で減点) | 0.10(負方向) |

**出力(1銘柄1行、predictions.csvに追記):**
- `gap_class`: ★★GAP / ★GAP / PASS / CAUTION の4段階(スコア閾値はconfig)。CAUTIONは負のEV=跨ぎ回避・空売り検討ゾーン。
- `ev_pred_pct`: 予想寄付ギャップ率。**定義を固定: 「発表前営業日終値 → 発表翌営業日寄付」の変化率**。
- `winrate_pred_pct`: 初期はスコアからの写像(較正テーブル)。実測が30件貯まったクラスから実測値に切替(M5参照)。
- `score_breakdown`: 因子ごとのスコアをJSONで保存(後の重み調整に必須)。
- `reason_quant`: 数値根拠の自動生成文(例: 「1Q進捗率31% vs 過去平均22%、過去3年で上方修正5回」)。
- `reason_qual`: Phase 2のLLMレイヤー用。v1では空欄可。

### M4: report.py
- Jinja2で `docs/index.html` を生成。デザインは参考イメージ(本命候補一覧表)を踏襲:
  - 列: # / コード / 会社名 / バッチ / EV / 勝率 / コンビクション / 評価 / GAP判定 / 評価が高い理由
  - EV降順ソート。★★GAP=赤バッジ、★GAP=橙、CAUTION=灰。
  - **最新バッチ(当日追加)の行は紫系ハイライト。** batch_id = run_date で判別。
  - 場中発表銘柄には⚠マークと発表時刻を表示。
- ページ下部に実測成績サマリ(stats.jsonから): クラス別の判定回数・実測勝率・平均ギャップ。**予想値と実測値を並べて表示し、乖離を可視化する。**
- 過去レポートは `docs/archive/YYYY-MM-DD.html` に保存。
- スマホ閲覧前提のレスポンシブ(閲覧はほぼスマホと想定)。

### M5: review.py
- 対象: `announce_date` が前営業日以前で `reviewed_at` が空の行。
- yfinanceから発表翌営業日の寄付・終値を取得し、以下を埋める:
  - `gap_open_pct`(寄付ギャップ=EVの答え), `gap_close_pct`(終値ベース), `ret_5d_pct`(5営業日後、スイング出口の参考。発表5営業日後に再訪して埋める)
  - `hit_flag`: EV予想の符号と gap_open_pct の符号が一致すれば1
- stats.json を再計算: gap_class別に n / 実測勝率 / 平均・中央値ギャップ / 標準偏差。**n≥30 のクラスは以後の winrate_pred をこの実測値で上書きする(較正)。**
- 大外れ(予想と実測の乖離が±10pt超)の銘柄は `notes` にフラグを立て、レポートに「要レビュー」として表示 → ここから rules.md に教訓を追記するのは人間(またはPhase 2のLLM)の仕事。

---

### 決算短信の型判定と上昇確度(earnings.py / reaction.py / site.py)
利用者が提示した「上がりやすい決算内容」の3つの型への親和性を、**TDnet の決算短信XBRLだけで**判定する(株探・IRBANK は GitHub Actions のIPを拒否するため使わない)。3つのうち1つでも該当すれば調査対象、複数該当は優先。

| 型 | 自動判定(config: earnings.types) | 未判定(Phase 2 の文章レイヤー) |
|---|---|---|
| ① リクルート型 | 今期累計の売上YoY − 前年同期の売上YoY(短信に載っている去年の伸び率)≥ 3pt かつ 営業利益率の前年同期差 ≥ 0。1Qは累計=単Qなので q-accel と同じ | 会社固有KPI(単価・課金率・継続率・ARR) |
| ② キオクシア型 | 単Q売上QoQ ≥ +20% かつ 営業益QoQ ≥ +50%(赤字→黒字含む)、前年の同じQoQより10pt以上強い(季節性除外)。単Q = 今回の累計 − 前回の短信の累計 | 製品価格の上昇、部門利益 |
| ③ ローツェ型B | 予想修正なし かつ 累計営業益YoY > 0 かつ 慎重度 ≤ 0.85。前期実績 = 会社予想 ÷ (1+予想の前期比)、慎重度 = (予想 − 累計) ÷ (前年の残り期間の実績 × 今期累計の伸び)。1Qで上期予想があれば上期で計算 | 経営陣の強気コメント |
| ③ ローツェ型A | ―(受注高はXBRLに無い) | 受注QoQ +20% |

- ②に必要な「前回の短信」は data/tdnet_earnings.csv に自前で蓄積する(TDnet は31日で消えるため、2026-09以降の分から)。前回の短信が揃っていない銘柄は②を判定しない(詳細表示にも出さない)。
- 上昇確度 = 同じバケット(決算は型の該当数 0/1/2以上、修正・配当は方向×幅)の過去開示で「開示前終値→開示後最初の終値」が上昇した割合を、全体の上昇率に prior_n 件ぶん寄せて縮小推定(reaction_stats.json)。型ごとの成績は types に参考として出す。
- 結果は data/earnings_features.csv(disclosure_id キー)。

### 決算説明資料(tdnet.py / irdocs.py / irsite.py)
- まず TDnet の「決算説明資料・補足資料・決算の概要・参考資料・ハイライト」等を短信にひも付ける(短信から14日以内)。決算と無関係な補足説明(資金調達・買収等)や説明会の開催案内・書き起こし・訂正は除外。
- TDnet に無い場合: 短信XBRLの「決算補足説明資料作成の有無」(SupplementalMaterialOf(Annual)Results)が「有」の会社だけ、短信XBRLに載っている会社URLから各社IRサイトをたどって説明資料PDFを探す(「無」は探さない)。
  - IR → 説明会・IR資料・ライブラリ → … と優先度つきで最大10ページ。PDFは「資料らしさ」「期(1Q〜通期)」「決算期(2027年2月期等)」「URLの日付」で点数づけ。短信そのもの・英語版・別の期は減点。
  - 普通に読んで見つからなければ Playwright(ヘッドレスChromium)で表示して探し直す(JavaScriptで一覧を作るサイト、iframe の外部IRサービス)。
  - robots.txt を守り1秒に1回まで。ボット拒否(Akamai 等)のサイトは突破しない。見つからなければ60分ごとに最大6回探し直す(説明資料は短信の数日後に載る会社が多い)。
  - 結果は data/ir_docs.csv、会社URLは data/company_urls.csv。watch の1分ごとの確認の合間に25秒ずつ進める。

### 行を開いたときの業績表(site.fin_panel / history.py)
- 上: 直近8四半期の単独値(売上・営業益・経常益・最終益・営業利益率)。過去分は data/quarterly_history.csv。最新の四半期は短信の累計 − 同じ期の前の四半期。
- 下: 今期の1Q〜通期の累計実績と会社予想(本決算の短信は来期予想)・進捗率。会社予想は tdnet_earnings.csv の fc_* 列(短信XBRL)。
- quarterly_history.csv の過去分は IRBANK 四半期進捗ページから一度だけ手元で取得(`history.bootstrap`、過去3年・約3,800社、source=irbank。IRBANK は Actions のIPを拒否するため手元で)。以降は earnings.update が短信ごとに当四半期を継ぎ足す(source=tdnet)。IRBANK のデータは出典明示で再配信可(サイトのフッターに表示)。
- コンセンサス・1株益の四半期推移は無料で安定して取れないため出さない。

### リアルタイム表示(watch.yml / feed.html.j2)
- watch.yml が平日 8:00〜20:00 JST に1分おきに TDnet を確認し、新しい開示があれば型判定・サイト再生成・push(ジョブ6時間制限のため 8:00〜14:00 と 14:00〜20:00 の2本)。daily.yml は watch の後(20:05)。
- トップページは docs/feed.json を1分ごとに読み直して自動更新。新着は NEW で強調、行タップで数値(売上・営業益YoY、加速、利益率、進捗率、QoQ、慎重度、修正率)を表示。
- 開示から表示までの遅れは、確認間隔(〜1分)+ Pages のデプロイ(〜1分)+ ページの再読込(〜1分)で数分。

## 4. predictions.csv スキーマ(確定)

```
# 判定時に書く列
run_date, batch_id, code, name, market, announce_date, announce_timing, fiscal_q,
score_total, score_breakdown, ev_pred_pct, winrate_pred_pct, conviction, gap_class,
reason_quant, reason_qual, price_at_pred,
# 答え合わせ時に埋める列
gap_open_pct, gap_close_pct, ret_5d_pct, hit_flag, reviewed_at, notes
```

このスキーマの変更は破壊的変更として扱い、変更時は必ずマイグレーションスクリプトを書くこと。

---

## 5. 実装フェーズ

### Phase 0 — MVP(最優先。7月末の海事関連Q1決算に間に合わせる)
- universe.csv 生成(M1)
- calendar.csv 生成(M2、株探補完は後回し可・J-Quantsのみでよい)
- judge: progress_gap と guidance_conservatism の2因子のみ(M3簡易版)
- predictions.csv 追記と最小限のHTMLレポート(M4簡易版)
- `--code` 単体判定(中国塗料4617、東京計器7721等で手動テスト)
- GitHub Actions daily.yml

### Phase 1 — 本運用
- 全5因子スコアリング、GAP判定4クラス、較正テーブル
- M5 答え合わせ自動化(review.yml)、stats.json、レポートへの実測成績表示
- 株探からの発表時刻取得と場中警告
- アーカイブページ

### Phase 2 — 精度向上
- **バックテスト**: J-Quantsの過去データで直近8四半期分の「擬似判定→擬似答え合わせ」を一括実行し、実運用を待たずに初期較正テーブルと重みを実データで作る。これが最も費用対効果の高い改善。
- LLM定性レイヤー: 判定対象銘柄のデータパック(直近開示タイトル一覧・スコア内訳)を組み立て、Anthropic APIで reason_qual と conviction 調整を生成。**LLMはスコアを±1クラスまでしか動かせない制約を入れる**(定量ロジックを感覚で上書きさせない)。
- コンセンサスプロバイダのプラガブル追加(取得可能な銘柄のみ progress_gap と併用)。
- ローカルStreamlit(パラメータいじりながら再計算する対話UI)。

---

## 6. 設計判断メモ(変更時はここを更新)

1. **コンセンサスをv1から外した理由**: 無料で安定取得できる日本株コンセンサスは存在しない(IFISは有料、Yahoo/みんかぶはカバレッジと規約の問題)。またスタンダード銘柄の多くはアナリストカバーが無く、コンセンサス依存の設計はユニバースの半分で機能しない。会社計画×進捗率×修正癖はコンセンサスの代理変数として機能し、全銘柄で計算可能。コンセンサスは「取れる銘柄だけ加点因子」としてPhase 2で足す。
2. **EVの定義を寄付ギャップに固定した理由**: 決算跨ぎの意思決定は「持ち越すか否か」であり、答えが出るのは翌日の寄付。終値や5日後は参考値として別列で持つ。定義が混ざると答え合わせが崩壊する。
3. **momentum_pricing を減点因子にした理由**: 決算前に既に大きく上げた銘柄は好決算が織り込み済みで、ギャップの期待値が下がる(出尽くしリスク)。乖離の評価は目的依存 — トレンドフォローなら乖離小が有利、リバ狙いなら乖離大が有利 — であり、決算跨ぎは「サプライズの未織り込み」を取る戦略なので事前上昇は減点が正しい。
4. **J-Quants Lightを前提にした理由**: 月1,650円で公式・構造化・遅延前日のデータが手に入る。スクレイピング依存を最小化でき、保守コストが劇的に下がる。ツールの信頼性の土台への投資として妥当。→ 2026-10 更新: 銘柄一覧・決算予定日はJPX公式Excelで十分な品質が得られたため無料ソースを一次に変更。
5. **LLMに判定の主導権を渡さない理由**: LLMの感覚値は検証も再現もできない。定量スコアが主、LLMは定性補正(±1クラス上限)と文章生成に限定。これで「なぜこの判定になったか」が常に score_breakdown で説明可能になる。
6. **修正開示はTDnetから準リアルタイム取得(GitHub Actions 15分おき)**: J-Quantsは当日夜〜翌日反映で、直前修正の検知に間に合わないことがある。TDnet一覧は約1ヶ月で消えるため disclosures.csv は洗い替えせず追記する。revision_recency の90日窓をTDnetだけで満たすには運用開始から約3ヶ月かかるので、それまでは `coverage_ok=false` を返す(J-Quantsでの過去分の補完はPhase 2)。決算短信と同日の修正は前回決算の一部とみなし、因子から除外する(config可変)。

---

## 7. 実装時の注意

- タイムゾーンは全てJST基準で扱い、GitHub Actionsのcron(UTC)との変換ミスに注意。営業日判定には `jpholiday` を使う。
- yfinanceの日本株データは欠損・分割未調整が起きうる。取得値の妥当性チェック(前日比±30%超は再取得)を入れる。
- 決算発表当日の18:30実行時点で、当日発表済み銘柄の短信データがJ-Quantsに未反映の可能性がある。M5は発表「翌営業日」の夜に走らせる設計とし、データ未反映時はスキップして次回再試行。
- pip install には `--break-system-packages` は不要(通常のvenv運用)。requirements.txt を管理する。
- テスト: judge のスコアリングはユニットテスト必須(固定の入力データに対する期待スコア)。データ取得層はモックでテスト。
7. **上昇確度を「型の該当数ごとの実測上昇率」にした理由**: 型の閾値は利用者の目安で、確度に直接写像する根拠が無い。該当数で集計した実際の株価反応を縮小推定で確度にすれば、感覚値を混ぜずに済み、運用とともに較正される(原則1と同じ考え方)。
