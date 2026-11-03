# テスト設計書との対応表

テスト設計仕様（RISU-local テスト設計仕様，2026-10-27）の各項目と，このリポジトリのテスト関数の対応．
「維持」は既存テストをそのまま登録したもの，「補強」は既存テストに加えて新しい関数を足したもの，
「新規」は今回追加したもの．件数は検証の単位で，関数の数ではない．

実行:

```powershell
pytest tests -q                                   # T / N（HTTP の契約も TestClient で毎回実行）
pytest tests/e2e -q                               # U（Playwright + Chromium が要る）
python scripts\evaluate_chat.py --cases L01-L12 --trials 3   # L（実 LLM，費用あり）
```

## 重点回帰 30 項目（§13）

| ID | 整備 | テスト |
|---|---|---|
| T01 | 新規 | `test_chat_intent.py::TestExplicitNoRun`（説明だけの依頼で実行しない．対照として再試行分岐が生きていることも確認） |
| T02 | 新規 | `test_chat_intent.py::TestModifyRequestKeepsBase`（基準からの差分実行 / 未完了通知 / 誤 ID・不正値でも基準不変） |
| T03 | 補強 | `test_reference_integrity.py::TestBaseReference`，既存 `test_llm.py::TestConversationContext` |
| T04 | 新規 | `test_provenance.py::TestDemandProvenanceSurvivesDerivation`（既存 `test_demand_provenance_is_injected` を派生後へ拡張） |
| T05 | 補強 | `test_reference_integrity.py::TestChartReferences::test_each_dataset_uses_its_own_sim`，既存 `test_llm.py::test_chart_data_refs_resolved` |
| T06 | 補強 | `test_chat_intent.py::TestFailuresAndPartialCompletion`（接続 / 認証 / レート制限 / 529 / UXsim タイムアウト / 不正引数），既存 `test_api_error_becomes_error_event_not_crash` |
| T07 | 補強 | `test_numerical_contracts.py::TestCapacityOnlyChange`，既存 `test_scenario_ops.py::test_base_scenario_not_mutated` / `test_rerun_handler_end_to_end` |
| T08 | 新規 | `test_reference_integrity.py::TestModificationSemantics::test_second_invalid_modification_fails_the_whole_rerun` |
| T09 | 新規 | 同 `test_demand_set_then_scale_in_order` |
| T10 | 新規 | 同 `test_shift_demands_keeps_total_vehicles`（需要の時間積分で 360 台） |
| T11 | 新規 | 同 `test_shift_into_negative_time_is_rejected` |
| T12 | 補強 | `test_api_contracts.py::TestUploadRobustness::test_json_envelope_roundtrip_keeps_params`，既存 `test_simulation.py::TestStandalonePipeline`，`test_frontend.py::TestGuiRerunCarriesScenarioParams` |
| T13 | 補強 | `test_chat_intent.py::TestWrongBaseIdIsNotSubstituted`，既存 `test_scenario_ops.py::test_rerun_handler_bad_sim_id` |
| T14 | 新規 | `test_reference_integrity.py::TestConversationIsolation::test_two_conversations_interleaved_keep_their_own_ids` |
| T15 | 維持 | `test_llm.py::TestLLMTokenSaving`（system / tools の固定，動的文脈は末尾 user，末尾の cache 境界） |
| T16 | 維持 | `test_tools.py::TestNetworkInfo`（limit ≤ 200，offset，絞込） |
| T17 | 補強 | `test_numerical_contracts.py::TestPlatoonAggregation::test_deltan_scaling_and_drawing_sample_invariance`，既存 `test_simulation.py::test_vehicle_sampling_when_over_point_limit` |
| T18 | 補強 | 同 `test_average_speed_is_vehicle_weighted`（25 m/s，20 台．リンク単純平均 20 m/s を返さない），既存 `test_aggregate.py::test_avg_speed_is_vehicle_weighted_and_shared` |
| T19 | 新規 | `test_reference_integrity.py::TestDemandComparison`（多重集合，`total_vehicles` 720 → 360） |
| T20 | 補強 | `test_numerical_contracts.py::TestClosure`，`test_reference_integrity.py::TestChartReferences::test_text_numbers_match_the_referenced_results`（完了 0 の平均は None） |
| T21 | 補強 | `test_reference_integrity.py::TestChartReferences::test_invalid_references_are_not_drawn_as_zero_traffic`（本体 `extract_charts` を通す），既存 `test_llm.py::TestChartExtraction` |
| T22 | 新規 | `test_reference_integrity.py::TestChartReferences::test_different_time_axes_are_aligned_explicitly` / `test_wrong_mapping_is_detectable` |
| T23 | 維持 | `test_tools.py::test_every_tool_use_gets_exactly_one_result` |
| T24 | 新規 | `test_chat_intent.py::TestFailuresAndPartialCompletion::test_round_limit_is_reported_as_incomplete` |
| T25 | 補強 | `test_api_contracts.py::TestCoreEndpoints`，`test_api.py::TestAPIEndpoints`（サーバー未起動なら TestClient に切替．skip しない） |
| T26 | 補強 | `test_api_contracts.py::TestUploadRobustness`（BOM・空セル・envelope・壊れた JSON 422・文字コード 422），既存 `test_importers.py::TestCSVParser` |
| T27 | 新規 | `test_api_contracts.py::TestGmnsUnits`（1 km / 72 km/時 / 1,800 台/時/車線 × 2 車線 = 1.0 台/秒．需要列の単位は列名で確定か仮定を通知） |
| T28 | 新規 | `test_api_contracts.py::TestNonFiniteInputs`（/simulate，/upload，ツール経路で NaN / Infinity を 422 / エラーに） |
| T29 | 維持 | `test_results.py::TestFrameWireEncodingV3` / `TestResultsEncodingNegotiation`，`tests/js/core.test.js` |
| T30 | 補強 | `test_provenance.py::TestProvenancePersists`，既存 `test_results.py::TestResultsPersistence` / `TestPersistenceRobustness` |

## 数値検証 8 モデル（§14）— `test_numerical_contracts.py`

| ID | テスト | 固定した許容差・実測 |
|---|---|---|
| N01 自由流 | `TestFreeFlow` | 旅行時間 200 秒 ± 4 秒（2 ステップ × 2 リンク）．実測 0.0 秒．総需要 30 台一致 |
| N02 内部ボトルネック | `TestInternalBottleneck` | AB 下流端の通過 ≤ 容量 × 窓 + 2 プラトン（60 / 120 / 300 秒窓）．実測 超過 0．下流閉塞・スピルバックなし |
| N03 閉鎖 | `TestClosure` | 到着 0，平均 None，残留車両を消さない |
| N04 需要 0・空白 | `TestDemandGaps` | 需要なし 0 台．2 バースト（UXsim の生成規則で 2 + 2 台）．500 / 1000 秒に走行車両なし，累積は 流入 = 到着 |
| N05 プラトン集計 | `TestPlatoonAggregation` | deltan 1 / 5 で生成規則どおり．描画間引きで統計・系列は不変．台数重み平均 25 m/s |
| N06 信号 | `TestSignalIntersection` | 停止線通過は実現示ログで青（± 1 ステップ）．実測 不一致 0．赤の最中の通過なし |
| N07 再現性 | `TestReproducibility` | 同 seed で統計・系列・timeline が一致．別 seed との一致は要求しない |
| N08 独立構築 | `TestIndependentConstruction` | UXsim 直接構築（analyzer）と total / completed 一致，平均 ± 0.05 秒．容量・信号・現示の対応 |

需要台数の基準は `demand_count`（UXsim の `adddemand` と同じ累積規則）．設定上の flow × 継続時間と
1 台ずれることがある（F1: 360 → 359）ので，検証はこの規則で行い，設定値との差は ≤ deltan を要求する．

## ブラウザ操作 12 シナリオ（§7）— `tests/e2e/test_chat_workflow.py`

偽 LLM をサーバープロセス内に差し込み（uvicorn は同一プロセスのスレッド），実 LLM は呼ばない．

| ID | テスト |
|---|---|
| U01 | `test_u01_chat_progress_result_and_chart`（進捗 → 結果 → 図．表示 ID = done.sim_id = ストアの新規 ID，操作可能な終端） |
| U02 | `test_u02_attach_file_edit_capacity_and_rerun`（添付 → EDIT で容量 0 → 実行．deltan / reaction_time / seed 維持，容量 0 保持） |
| U03 | `test_u03_save_new_restore_from_history` / `test_u03_restore_from_uploaded_envelope` / `test_u03_uploaded_envelope_keeps_link_geometry`（保存 → NEW → 再読込．条件・統計・道路形状・出所を復元） |
| U04 | `test_u04_stop_during_response_then_resend`（STOP 後に入力可能，旧応答が新結果を上書きしない．サーバー計算の停止は保証しない） |
| U05 | `test_u05_new_during_response_keeps_new_conversation_clean`（応答中に NEW．旧会話の遅延 done が新会話の表示・ID を変えない） |
| U06 | `test_u06_demand_gap_and_after_arrivals`，既存 `test_browser_smoke.py::test_demand_gap_shows_no_vehicles` |
| U07 | `test_u07_sse_split_at_arbitrary_bytes`（SSE を 7 バイトごとに分割配信．日本語・複数イベント・最後のチャンク） |
| U08 | `test_u08_invalid_chart_and_html_are_rendered_safely`（壊れた chart・HTML を含む名前と回答．スクリプト非実行，注記表示） |
| U09 | `test_u09_ab_comparison_chart_then_switch_result`（時間軸の整列された比較図，結果の切替で図の出所と表示 ID を混同しない） |
| U10 | `test_u10_redisplay_previous_result_and_modify`（履歴から再表示 → 修正．/chat の last_sim_id と新結果の base_sim_id が再表示した ID） |
| U11 | `test_u11_draw_modes_and_signal_boundary`，既存 `test_browser_smoke.py::test_signals_are_drawn_as_stop_bars` / `test_result_loads_and_all_draw_modes_render` |
| U12 | `test_u12_bad_file_then_network_error_then_recovery`（壊れたファイル 422 → 通信エラー → 正しい入力で回復，既存結果の閲覧可） |

## 実 LLM 評価 26 タスク（§4 / §15）— `evals/cases/L01〜L26.json`，`scripts/evaluate_chat.py`

判定器は `scripts/evaluate_chat.py` の `check_L01`〜`check_L26`．`--selfcheck` が §16.4 の
「誤りを入れたデータで不合格を返す」確認（L01 容量 1800 / L05 BC 変更 / L06 S1 からの派生 /
L12 区間長違い / L08 時刻対応の誤り / L11 平均 0 秒 / L07 実行してしまう）．
記録は `evals/runs/<日時>/`（§16.3 の形式．gitignore）．2026-10-27 の 36 / 72 会話の集計と判断は `evals/RESULTS.md`．

## 設計書の指摘と本体側の修正

| 指摘 | 修正 |
|---|---|
| 「交通」等のキーワードで `run_simulation` を強制再試行（L07 と衝突） | `risu/llm.py`: `NO_RUN_PATTERNS` に当たれば再試行しない．再試行は `tool_choice=any` で，表示中の結果があれば rerun を案内 |
| 派生結果に `source.demand` を引き継がない | `risu/tools.py`: `derived_demand_provenance`（引き継ぎ + 需要を変えた命令の記録）．tool_result に `demand_assumption` |
| `compare_simulations` の需要は集合・flow の和 | `risu/aggregate.py`: 多重集合（Counter），`n_rows`，`total_vehicles`（flow × 継続時間） |
| 起動済みサーバーが無いと API 試験が skip | `tests/test_api.py`: TestClient へ切替．`tests/test_api_contracts.py` を追加 |
| 無効な `$data` 参照を空配列にして描いていた | `risu/llm.py`: データセットを外して本文に注記．ラベル不明・全滅ならグラフを省く．壊れた JSON も注記 |
| 異なる時間軸の sim を同じラベルに重ねていた | `risu/llm.py`: 時刻の和集合に整列（無い時刻は null，`options.plugins.risu.alignedTimeAxis`） |
| ラウンド上限で未実行の要求が黙って落ちる | `risu/llm.py`: 本文末尾に未完了の注記，done に `incomplete: true` |
| 壊れた JSON / 非 UTF-8 のアップロードが 500 | `risu/api.py`: 422 で位置を返す |
| NaN / Infinity が UXsim まで届く，422 の応答が直列化で落ちる | `risu/schema.py`: `allow_inf_nan=False`．`risu/api.py`: 検証エラーの detail を JSON 安全化 |
| GMNS の需要単位が値の大小だけで決まる | `risu/importers.py`: 列名で確定（`flow` = 台/秒，`volume_per_hour` 等 = 台/時），判別不能なら `notes` で仮定を通知 |
| 需要の空白時間に前のフレームの累積を引きずる（走行中 1 台と表示） | `risu/simulation.py`: `trip_series` の時間軸に各空白の始まりを含める |
| 保存した `.json+result` / `.json` を再アップロードすると多点の道路形状が直線に戻る（U03 で発見） | `risu/results.py`: エンベロープの scenario に `link_geometries` を同梱（`link_geometries_of`）．読み戻し時は正規化シナリオから外す |
| `update_demands` に未知のキー（match / filter / names / remove）や無内容の命令を渡すと，黙って「全需要に何も設定しない」で成功扱い（実 LLM 評価 L13 / L22 で発見） | `risu/scenario_ops.py`: set / scale_flow 必須，未知キーはエラーにして orig / dest と remove_demands を案内．`risu/prompts.py`: OD 指定・`remove_demands`・`remove_nodes` の例と「対象が一意でなければ確認する」を追記 |
| 応答中に NEW すると旧会話の done が新会話に吹き出しと表示 ID を混ぜる（U05 で発見） | `static/js/app.js` / `ui.js` / `chat.js`: 会話の世代 `chatGeneration`．NEW は進行中の要求を中止して世代を進め，送信時と世代が違うイベントは捨てる |
