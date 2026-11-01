# Referência do config.json (versão 4.0)

O `config.json` na pasta do projecto. Chaves em falta usam a omissão indicada. Chaves
desconhecidas são recusadas com uma mensagem que diz qual é. Chaves que começam por `_`
(por exemplo `_legacy_v30` ou `_comentario`) são ignoradas.

**Recarga automática:** o bot relê o ficheiro a cada `config_reload_seconds` (60 s). Um
ficheiro inválido é ignorado (o bot continua com a configuração anterior e diz porquê).
Chaves de segurança só recarregam no sentido **mais restritivo**; alargá-las, mudar para um
perfil mais agressivo ou mudar as chaves marcadas com "reinício" exige reiniciar o bot.

## 1. Conta e mercado

| Chave | Omissão | Descrição |
|---|---|---|
| `config_version` | `"3.1"` | Versão do esquema. Ficheiros antigos são migrados com avisos (ver [UPGRADE.md](UPGRADE.md)). |
| `symbols` | `["EURUSD"]` | Símbolos base, sempre no universo (reinício). Sufixos da corretora são detectados. |
| `timeframe` | `"M15"` | Período principal: o da equipa e do ecrã do escritório (reinício). |
| `bars` | `1000` | Velas fechadas lidas por período em cada ciclo. |
| `history_months` | `12` | Histórico pedido para a investigação semanal. |
| `magic` | `510001` | Identificador das ordens do bot (reinício). Uma instância por `magic`. |
| `account_mode` | `"any"` | `any`, `demo_only` ou `real_only` (reinício). |
| `real_money_confirmed` | `false` | Confirmação permanente para contas reais (reinício). |
| `mt5_path` | `null` | Caminho do `terminal64.exe` se não estiver em Program Files (reinício). |

## 2. Risco

| Chave | Omissão | Descrição |
|---|---|---|
| `risk_profile` | `"balanced"` | `conservative`, `balanced` ou `aggressive`. Tabela completa em [EVIDENCE_AND_RISK.md](EVIDENCE_AND_RISK.md#5-perfis-de-risco). |
| `risk_per_trade_pct_demo` | `null` | Tecto opcional do risco por trade em demo (só aperta o perfil). |
| `risk_per_trade_pct_real` | `null` | Igual, em conta real. |
| `max_daily_loss_pct_demo` / `_real` | `null` | Tecto opcional da perda diária. |
| `max_open_positions_total` | `null` | Tecto opcional de posições abertas. |
| `max_total_open_risk_pct` | `null` | Tecto opcional do risco aberto total. |
| `max_drawdown_from_peak_pct` | `null` | Tecto opcional do disjuntor de drawdown. |
| `max_symbol_exposure_x` | `5.0` | Exposição nocional máxima por símbolo, em múltiplos do capital (tecto 10). |

Todas estas chaves só podem **apertar**: o valor efectivo é o mínimo entre o perfil, a sua
chave e o tecto rígido do código.

## 3. Execução e filtros

| Chave | Omissão | Descrição |
|---|---|---|
| `trading_hours_server` | `[2, 22]` | Janela de entradas na hora do **servidor** da corretora. O Doutor de Fluxo pode alargá-la até 4 h, nunca fora de 1h a 23h. |
| `deviation_points` | `10` | Desvio máximo aceite no envio. |
| `max_slippage_points` | `20` | Acima disto, as entradas nesse símbolo param `slippage_pause_minutes` (60). |
| `close_on_opposite` | `true` | Fecha uma posição quando o braço que a abriu dá o sinal oposto. |
| `commission_per_lot` | `null` | Comissão por lote (ida e volta). `null` detecta no histórico; sem histórico usa `commission_fallback_per_lot` (7,0). Em contas só com spread, `0`. |
| `backtest_slippage_points` | `2.0` | Deslizamento assumido em sombra e investigação. |
| `news_filter_enabled` | `false` | Janela sem entradas à volta das notícias de `news_file` (`news.csv`), `news_minutes_before`/`after` (30/30). |

## 4. Universo: bloco `universe` (reinício)

| Chave | Omissão | Descrição |
|---|---|---|
| `candidates` | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD, EURJPY, XAUUSD | Símbolos que o explorador de oportunidades pode juntar aos de `symbols`. |
| `timeframes` | `["M5", "M15", "H1"]` | Períodos dos braços. |
| `max_symbols` | `6` | Universo máximo (sem contar os extra do Doutor de Fluxo). |
| `max_arms` | `400` | Braços activos no máximo. |
| `default_arms` | `true` | Cria um braço com parâmetros por omissão por estratégia, símbolo e período. |
| `max_spread_to_stop` | `0.25` | Spread máximo como fracção da distância ao SL (válido em qualquer mercado). |
| `spread_spike_mult` | `2.5` | Spread máximo face ao típico daquela hora. |
| `max_spread_points_by_symbol` | `{}` | Tectos absolutos opcionais em pontos, por exemplo `{"XAUUSD": 60}`. |
| `scan_every_minutes` | `15` | Intervalo do explorador de oportunidades e do mapa de viabilidade. |

## 5. Níveis e exploração: bloco `tiers`

| Chave | Omissão | Descrição |
|---|---|---|
| `t1_enter_p` / `t1_exit_p` | 0,55 / 0,50 | Probabilidade P(E>0) para entrar e sair de T1. |
| `t1_min_evidence` | `20` | Trades de evidência própria para T1 sem exploração. |
| `t2_enter_p` / `t2_exit_p` / `t2_min_trades` / `t2_max_dd_r` | 0,80 / 0,70 / 50 / 6 | Regras de T2. |
| `t3_enter_p` / `t3_exit_p` / `t3_min_trades` / `t3_max_dd_r` | 0,93 / 0,85 / 100 / 12 | Regras de T3. |
| `promote_cooldown_hours` | `4` | Intervalo mínimo entre subidas do mesmo braço (descidas imediatas). |
| `exploration_enabled` | `true` | Trades de exploração a partir de T0. |
| `exploration_slots` | `2` | Trades de exploração abertos ao mesmo tempo. |
| `exploration_threshold` | `0.0` | O sorteio de Thompson tem de passar este valor (em R). |
| `exploration_day_pct` / `exploration_week_pct` / `t1_risk_pct` | `null` | Tectos opcionais (só apertam o perfil). |
| `prior_discount` | `0.3` | Peso dos trades da investigação na evidência. |
| `global_mean` | `-0.03` | Média céptica (R por trade) quando não há evidência. |
| `tau_min` | `0.05` | Dispersão mínima entre braços de uma família. |
| `evidence_refresh_seconds` | `120` | Intervalo de recálculo da evidência. |

## 6. Capital: bloco `capital`

| Chave | Omissão | Descrição |
|---|---|---|
| `floor_drawdown_pct` | perfil | Distância inicial do piso ao capital inicial. |
| `lock_fraction` | perfil | Parte de cada novo máximo protegida (tecto 0,9). |
| `house_money_max` | perfil | Multiplicador máximo com lucros já ganhos (tecto 1,5). |
| `kelly_fraction` | perfil | Fracção de Kelly em T2/T3 (tecto 0,5). |
| `affordability_cap_pct` | perfil | Risco máximo aceite para o lote mínimo como excepção, em T2/T3. |
| `floor_soft_stop` | `true` | Pára as entradas no dia em que o capital toca o piso. |

## 7. Doutor de Fluxo: bloco `flow_doctor`

| Chave | Omissão | Descrição |
|---|---|---|
| `enabled` | `true` | Liga o governador de actividade. |
| `max_idle_hours_demo` / `_real` | 6 / 12 | Horas sem ordens (com sinais e mercado aberto) antes de agir. |
| `review_after_trades` / `review_after_hours` | 10 / 48 | Quando um ajuste é revisto. |
| `revert_mean_r` | `-0.25` | Média em R abaixo da qual um ajuste é revertido. |
| `tighten_daily_loss_used` | `0.6` | Fracção da perda diária a partir da qual aperta. |
| `min_hours_between_adjustments` | `2` | Intervalo mínimo entre ajustes. |
| `max_relaxations_per_week` | `3` | Máximo de alívios por semana. Reverter e apertar nunca têm limite. |
| `min_counterfactual_signals` | `10` | Sinais bloqueados com repetição contrafactual exigidos antes de aliviar. |
| `min_counterfactual_r` | `0.0` | Média em R que esses sinais têm de atingir. |

## 8. Gestão de posições: bloco `management`

| Chave | Omissão | Descrição |
|---|---|---|
| `policies_enabled` | `true` | Políticas de gestão por braço, escolhidas por evidência. Com `false` todos usam SL/TP fixos. |
| `policy_min_trades` | `30` | Trades emparelhados para mudar de política. |
| `policy_min_p_better` | `0.6` | Probabilidade de a nova política ser melhor. |
| `spread_spike_protect` | `true` | Adia ajustes de stop durante picos de spread. |
| `news_derisk_minutes` | `15` | Antes de notícias de alto impacto, stop para a entrada em posições com +0,5R (`null` desliga). |

As regras manuais antigas continuam disponíveis em `team.position_manager`
(`breakeven_after_r`, `trailing_atr_mult`, `time_stop_bars`, `partial_close_at_r`,
`partial_close_fraction`, `pre_news_close_minutes`), desligadas por omissão; somam-se às
políticas e também só apertam.

## 9. Investigação: bloco `research` e chaves de optimização

| Chave | Omissão | Descrição |
|---|---|---|
| `research.startup_trials` | `8` | Ensaios por estratégia na investigação de arranque (rápida). |
| `research.startup_history_months` | `6` | Histórico da investigação de arranque. |
| `research.retry_minutes` | `60` | Nova tentativa depois de uma investigação falhada. |
| `research.reference_equity` / `reference_risk_pct` | 10 000 / 1,0 | Capital e risco de referência da sombra e da investigação (resultados em R independentes da conta). |
| `trials_per_strategy` / `max_trials_per_cycle` | 30 / 300 | Ensaios da investigação semanal. |
| `optimizer_workers` | `1` (o ficheiro de exemplo usa 2) | Processos da investigação, em baixa prioridade. |
| `wf_train_months` / `wf_test_months` / `wf_step_months` | 3 / 1 / 1 | Janelas walk-forward. |
| `reoptimize_every_days` | `7` | Investigação semanal (corre ao fim de semana ou quando atrasada). |
| `enabled_strategies` | `null` | Lista de estratégias permitidas; `null` usa todas. |

## 10. Operação

| Chave | Omissão | Descrição |
|---|---|---|
| `dry_run` | `false` | Tudo menos enviar ordens. |
| `poll_seconds` / `closed_market_poll_seconds` | 5 / 60 | Ritmo do ciclo. |
| `stale_tick_minutes` | `5` | Sem ticks há mais do que isto: mercado considerado parado. |
| `terminal_restart_enabled` / `terminal_freeze_minutes` | `true` / 10 | Reinício de um terminal congelado (só com todas as posições protegidas). |
| `holidays` | `[]` | Datas sem mercado, para a detecção de terminal congelado. |
| `heartbeat_minutes` | `30` | Linha de vida no `bot.log`. |
| `telegram_enabled` | `false` | Notificações (variáveis `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`). |
| `kill_switch_file` | `"KILL"` | Ficheiro que fecha as posições do bot e pára. |
| `state_root` / `reports_dir` / `health_file` / `log_file` | `state` / `reports` / `health.json` / `bot.log` | Pastas e ficheiros (reinício). |

## 11. Blocos `team`, `claude`, `news`, `panel`, `notifications`

Iguais aos da versão 3.0, com estas notas:

* `team.team_mode`: `auto` (omissão: a equipa só reduz depois de provar valor fora da
  amostra), `full` ou `baseline` (a equipa só regista opiniões). A equipa tem 22 agentes,
  incluindo o Doutor de Fluxo.
* `claude.enabled` é `false` por omissão; sem chave o bot funciona igual. Limites:
  `max_calls_per_hour` (20), `max_daily_cost_usd` (0,50), `max_monthly_cost_usd` (5,00),
  `latency_budget_ms` (1500).
* `claude.review_trades` é `false` por omissão. Com `true`, as revisões ficam em sombra até a
  ablação mostrar valor depois do custo (`review_requires_ablation`, `true`).
  `claude.batch_reports` (`true`) usa processamento em lote nos relatórios.
* `panel`: `host` `127.0.0.1`, `port` 8765, `control_actions_enabled` `false`,
  `token_env` `MT5BOT_PANEL_TOKEN`.

## 12. Execução: bloco `execution`

O que nenhuma configuração muda: uma entrada incerta nunca é reenviada.

| Chave | Omissão | Descrição |
|---|---|---|
| `max_attempts` | `3` | Tentativas em rejeições transitórias. |
| `backoff_seconds` / `backoff_max_seconds` | `0.5` / `8` | Recuo exponencial entre tentativas. |
| `confirm_polls` / `confirm_poll_seconds` | `6` / `0.25` | Espera limitada por uma confirmação tardia. |
| `uncertain_grace_seconds` | `120` | Tempo sem nada no corretor para provar que não executou. |
| `protect_attempts` / `protect_retry_seconds` | `3` / `0.5` | Tentativas de anexar SL e TP. Depois, fecha. |
| `stop_tolerance_points` | `2` | Tolerância ao comparar stops. |
| `retryable_retcodes` | `[10004, 10020, 10021, 10024]` | Só aceita não execuções definitivas; o resto é rejeitado ao carregar. |
| `breaker_failures` / `breaker_open_seconds` / `breaker_half_open_probes` | `5` / `300` / `1` | Disjuntor do corretor. |

## 13. Custos: bloco `costs`

| Chave | Omissão | Descrição |
|---|---|---|
| `k` | `2.0` | Vantagem exigida em múltiplos do custo total. Limites 1 a 10. |
| `edge_z` | `1.645` | Que quantil inferior conta como vantagem provada: 1,645 é o quantil de 5%, 0,67 é o de 25%. Limites 0,67 a 2,33. Valores mais baixos negoceiam mais e provam menos. |
| `gate_enabled` | `true` | Porta de custo e guardas de calendário. |
| `exploration_exempt_demo` / `_real` | `true` / `false` | Exploração T1 isenta da porta. |
| `expected_hold_bars` | `20` | Barras de permanência assumidas para swap e gaps. |
| `calibrate_every_hours` | `24` | Reajuste do modelo de custos. |
| `rollover_start` / `rollover_end` | `"23:50"` / `"00:20"` | Hora do servidor. O que se aprende com os dados só pode alargar a janela. |
| `friday_cutoff_hour` | `21.0` | Sem novas entradas a partir daqui. |
| `flat_before_weekend` | `true` | Contas pequenas fecham antes do fim de semana, salvo braços T3. |

## 14. Carteira: bloco `portfolio`

Tudo aqui só reduz posições.

| Chave | Omissão | Descrição |
|---|---|---|
| `max_concurrent_arms` | `4` | Braços abertos ao mesmo tempo. Máximo 5. |
| `correlation_cap` | `0.7` | Correlação a partir da qual dois símbolos são um grupo. |
| `max_cluster_weight` | `0.5` | Peso máximo de um grupo. |
| `max_currency_risk_pct` | `2.0` | Risco máximo por moeda. |
| `target_correlated_risk_pct` | `2.5` | Alvo de risco da carteira, com correlação. |

## 15. Cauda: bloco `tail`

| Chave | Omissão | Descrição |
|---|---|---|
| `margin_stop_out_multiple` | `3.0` | Nível de margem projectado mínimo, em múltiplos do stop-out. Mínimo 2. |
| `margin_call_multiple` | `1.5` | Abaixo disto em relação à chamada, novas entradas param. |
| `gap_quantile` | `0.99` | Quantil dos gaps históricos usado no tamanho. |
| `overnight_hours` | `6` | Permanência a partir da qual se conta com gaps. |
| `small_account_equity` | `25000` | Abaixo disto, sem posições ao fim de semana. |
| `ladder_recovery_days` | `3` | Dias entre subidas na escada de risco. |
| `stress_reduction` | `0.5` | Multiplicador de risco novo depois de um cenário violar limites. |

## 16. Governo: bloco `governance`

| Chave | Omissão | Descrição |
|---|---|---|
| `enabled` | `true` | Regra de paragem da conta real. |
| `horizon_trades` / `horizon_days` | `200` / `91` | Horizonte de avaliação: o que vier primeiro. |
| `rolling_min_trades` | `30` | Trades em T2 ou acima para haver veredicto. |
| `confidence` | `0.95` | Confiança do intervalo. |

## 17. Investigação com rigor: chaves novas em `research`

| Chave | Omissão | Descrição |
|---|---|---|
| `cpcv_groups` / `cpcv_test_groups` | `6` / `2` | Divisão da CPCV. |
| `embargo_bars` | `20` | Barras de embargo depois de cada bloco de teste. |
| `min_dsr` / `max_pbo` | `0.95` / `0.5` | Limites do veredicto. |
| `min_trades` | `30` | Trades para haver veredicto. |
| `require_verdict` | `true` | T2 exige "vantagem plausível". |
| `retire_failed` | `true` | Braços sem vantagem voltam à investigação. |
| `capture_ticks` / `tick_retention_days` | `true` / `30` | Captura contínua de ticks e barras. |

## 18. Operações: bloco `ops`

| Chave | Omissão | Descrição |
|---|---|---|
| `cycle_budget_ms` | `2000` | Orçamento por ciclo; acima dele saltam etapas opcionais. |
| `json_logs` | `false` | Registos estruturados com id de correlação. |
| `metrics_enabled` | `false` | `/metrics` no formato Prometheus, no painel. |
| `clock_drift_warn_seconds` | `5` | Alerta de relógio. |
| `ntp_server` | `null` | Referência NTP opcional. |
| `backup_enabled` / `backup_hour_server` / `backup_keep_days` | `true` / `3` / `14` | Cópia diária verificada por restauração. |
| `symbol_info_ttl_seconds` | `30` | Cache de especificações do símbolo no terminal real. |

## Exemplo mínimo

```json
{
  "config_version": "3.1",
  "symbols": ["EURUSD"],
  "risk_profile": "conservative",
  "trading_hours_server": [2, 22],
  "universe": {"max_symbols": 4},
  "panel": {"enabled": true}
}
```
