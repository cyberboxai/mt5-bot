# Configuração v4.2

Referência das chaves novas, migração automática e configuração recomendada. A referência geral continua em `docs/CONFIG.md`.

## Migração da configuração

Ao arrancar, se `config.json` estiver num formato antigo, o bot guarda uma cópia datada (`config.json.bak-AAAAMMDD-HHMMSS`) e reescreve o ficheiro no formato actual. Uma cópia nunca é apagada. O que muda:

| Antes | Depois |
|---|---|
| `account_mode: "any"` com `real_money_confirmed: true` | `account_mode: "real"` |
| `account_mode: "any"` com `real_money_confirmed: false` | `account_mode: "auto"`, continua sem confirmação |
| `demo_only`, `real_only` | `demo`, `real` |
| `symbols` | entra em `universe.candidates`; o saldo decide quais são negociados |
| `timeframe` | entra em `universe.timeframes` |
| `tail.small_account_equity` | removida; guardada em `_legacy_v30` |

**Uma migração nunca liga dinheiro real.** Se `real_money_confirmed` era `false`, continua `false`.

A sua configuração migrada fica com: `account_mode: "real"`, `real_money_confirmed: true`, `risk_profile: "aggressive"`, e BTCUSD acrescentado aos candidatos. Mantém `exploration_threshold: -0.05` porque foi escolha sua; o novo valor por omissão é `-0.02`, mais prudente.

## Configuração recomendada para o saldo detectado

```
python mt5_bot.py --recommend-config
```

Faz a verificação prévia e escreve `config.recommended.json` ao lado de `config.json`. **Nunca é aplicada sozinha:** leia-a e copie o que quiser. Com o saldo inferido de cerca de 65 USD espera-se:

| Chave | Valor esperado | Porquê |
|---|---|---|
| `universe.candidates` | EURUSD, BTCUSD e os majors viáveis | o lote mínimo cabe no limite |
| `universe.exclude` | XAUUSD | 1 onça arrisca 9,6 % ou mais |
| `universe.timeframes` | M5, M15 | H1 só onde couber |
| `risk_profile` | `aggressive` | escolha do proprietário |
| `sizing.min_viable_enabled` | `true` | sem isto nenhum trade é possível neste saldo |

Com outro saldo o resultado é diferente: tudo vem das especificações da corretora, nada de bandas fixas.

## Perfis de risco

Percentagens do capital. Todos ficam abaixo dos tectos rígidos: 2 % por trade, 5 % por dia, 20 % de drawdown, 4 % de risco aberto.

| Número | conservative | balanced | aggressive |
|---|---|---|---|
| Risco T1 | 0,10 | 0,25 | 0,50 |
| Risco T2 | 0,25 | 0,50 | 1,00 |
| Risco T3 | 0,50 | 1,00 | 2,00 |
| Limite do tamanho mínimo viável | 0,75 | 1,25 | 2,00 |
| Perda diária | 2,5 | 3,5 | 5,0 |
| Limite semanal suave | 5,0 | 7,0 | 10,0 |
| Risco aberto total | 1,5 | 2,5 | 4,0 |
| Disjuntor de drawdown | 8 | 12 | 18 |
| Trades de exploração por dia | 2 | 3 | 4 |
| Trades de exploração por semana | 6 | 9 | 12 |
| Bloqueio de novos máximos | 50 % | 40 % | 30 % |

## Chaves novas

| Chave | Omissão | Limites | Nota |
|---|---|---|---|
| `account_mode` | `"real"` | `real`, `demo`, `auto` | `--account` tem prioridade |
| `sizing.min_viable_enabled` | `true` | | desligado, um lote mínimo acima do risco do nível é recusado |
| `sizing.min_viable_risk_cap_pct` | do perfil | 0,01 a 2,0 | só aperta |
| `budget.min_trades_per_day` | do perfil | 0 a 20 | limitado pela perda diária |
| `budget.min_trades_per_week` | do perfil | 0 a 60 | limitado pelo limite semanal |
| `budget.weekly_soft_limit_pct` | do perfil | 0,1 a 10 | depois disto, sem novas entradas até à semana seguinte |
| `startup.research_sprint_minutes` | `20` | 1 a 240 | tempo máximo da investigação inicial |
| `startup.sprint_trials` | `6` | 1 a 100 | ensaios por estratégia |
| `startup.ack_max_age_days` | `30` | 0 a 3650 | ausência que volta a pedir a contagem |
| `entry_gates.min_mean_r` | `0.0` | 0 a 1 | expectativa depois de custos |
| `entry_gates.min_p` | `0.55` | 0,52 a 0,99 | P(E>0) |
| `entry_gates.min_cost_to_edge` | `1.2` | 1,0 a 20 | sobre a média posterior |
| `entry_gates.min_oos_trades` | `30` | 20 ou mais | trades fora da amostra |
| `scaling_gates.min_cost_to_edge` | `2.0` | 1 a 20 | sobre o limite inferior |
| `scaling_gates.require_verdict` | `true` | | exige veredicto de investigação (Sharpe deflacionado e PBO) |
| `scaling_gates.max_reality_gap_r` | `0.15` | 0,01 a 5 | atraso dos trades reais face à simulação que congela promoções, em R por trade |
| `learning.half_life_days` | `60` | 1 a 3650 | meia-vida da evidência |
| `learning.drift_detector` | `"page_hinkley"` | `page_hinkley`, `cusum`, `off` | |
| `learning.drift_delta_r` | `0.15` | 0 a 2 | queda ignorada, em R |
| `learning.drift_threshold_r` | `15` | 0,5 a 200 | limiar do alarme |
| `learning.drift_min_trades` | `30` | 5 ou mais | |
| `learning.nightly_research` | `true` | | ciclo nocturno |
| `learning.nightly_hour_server` | `23` | 0 a 23 | hora do servidor |
| `learning.stop_variants` | `true` | | um braço por política de stop |
| `slo.first_trade_hours` | `2.0` | 0,25 a 168 | horas de mercado activo com braço elegível |
| `capacity.max_child_orders` | `4` | 1 a 4 | ordens filhas por entrada |
| `capacity.child_interval_seconds` | `30` | 0 a 3600 | intervalo entre ordens filhas |
| `capacity.lots_per_tick` | `0.25` | 0,001 a 100 | heurística: o MT5 não dá volume real em FX |
| `capacity.hysteresis` | `0.1` | 0 a 0,5 | banda contra mudanças de modo |
| `universe.always_include` | `[]` | | símbolos sempre seguidos |
| `universe.exclude` | `[]` | | símbolos nunca seguidos |
| `universe.max_cost_r` | `0.5` | 0,02 a 2 | custo máximo em R antes de haver vantagem medida |

## Comissão

`commission_per_lot: 0.0` com `commission_fallback_per_lot: 7.0` não é verificável aqui. O bot passou a ler a comissão real do histórico de negócios e a usá-la no modelo de custos. Se o valor configurado for diferente, escreve no registo:

```
EURUSD: commission_per_lot in the config is 0.00 but the deal history shows 7.00 per lot; using the history
```

Sem histórico, usa `commission_per_lot`; sem esse valor, a reserva de 7,0 por lote, que é pessimista.
