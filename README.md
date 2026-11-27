# nanachi-watch

ぴよの道具の新版・株価の異変・埼玉県の気象警報を監視して、
MuseKnock のカスタム Webhook へ通知する小さな監視スクリプト。

- `watch.py` — 本体。モード: releases / stocks / fund / weather / test
- `.github/workflows/watch.yml` — スケジュール実行（このファイルは workflow.yml として配置）
- `state.json` — 前回状態。ワークフローが自動でコミットして更新する。
  初回実行はシードのみで通知しない（過去分で溢れないため）。

## 監視対象
- リリース: Obsidian (obsidian-releases), Trafilatura, MuseLink, last30days-skill — 毎日 09:00 JST
- 株価: クレハ 4023.T / アステラス 4503.T / INPEX 1605.T
  （前日比 ±3% または 5日比 ±5% で通知）— 平日 16:30 JST
- 気象警報: 気象庁 埼玉県 (110000) の警報級のみ — 30分ごと
- ファンド: インベスコ 世界厳選株式＜H無＞（毎月決算型）「世界のベスト」の基準価額
  （前日比 ±3% または 5日比 ±5%、分配金調整後で通知）— 平日 22:30 JST

## 必要な Secret
- `HOOK_TOKEN` — MuseKnock Worker の `HOOK_TOKEN` と同じ値。
