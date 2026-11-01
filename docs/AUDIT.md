# Auditoria técnica (v3.1 para v4)

Medido em 2026-11-01 sobre o commit `821e3b7`, antes de qualquer alteração ao código. Ferramentas: `tools/audit_arch.py` (grafo e linhas), `tools/benchmark.py` (perfil e tempos) e `coverage`. O que não foi medido está dito como tal.

## 1. Resumo

- O sistema estava bem testado (980 testes, 82% de cobertura) e sem ciclos de importação.
- O risco maior estava na execução: uma ordem sem resposta contava como "não executada". Foi a correcção mais importante.
- **Não há histórico real nesta máquina.** O relatório de realidade (secção 5) não tem números, e nenhum componente tem valor medido em dados reais.
- A execução e a evidência ganharam rigor, e o código de execução saiu do `Bot`. Remover componentes sem dados seria um palpite, por isso a equipa continua, mas fora das decisões por omissão.

## 2. Arquitectura

| Medida | v3.1 |
|---|---|
| Linhas de código (sem comentários nem vazias) | 18 415 em 97 módulos |
| Maior módulo | `mt5bot/bot.py`, 2091 linhas |
| Seguintes | `panel/data.py` 811, `cli.py` 604, `arms.py` 602, `config_blocks.py` 578, `risk.py` 503 |
| Ciclos de importação ao nível do módulo | 0 |
| Importações de `MetaTrader5` | 1 (`bot.py`, dentro do construtor) |
| Instrução `global` | 1 (`optimizer.py`, prioridade do processo) |
| `sleep` bloqueante no ciclo | 0 directos; o ciclo usa a espera injectada |
| Pontos que tocavam o terminal | 95 (`bot.py` 80, `teambridge.py` 10, `cli.py` 3, testes 2) |
| Testes e cobertura | 980 passaram, 6 ignorados, 82% global |

**Lógica duplicada.** A construção do pedido de ordem existia em três sítios (entrada, fecho, alteração de stops), e a verificação de retcodes em dois. O comentário de ordem era uma constante única, sem identificar a ordem.

**Código morto.** Foram removidas 24 funções sem chamadas em produção nem em testes, 19 herdadas da v3.1 e 5 escritas durante a v4 e nunca usadas, 146 linhas no total, sobretudo restos de campeão e desafiante da v3.0 em `registry.py`. `sim/terminal.py` (287 linhas) foi substituído por `SimExchange`.

**Estado global.** A cache de indicadores e o sinalizador de prioridade do optimizador. Ambos inofensivos para decisões.

**Bloqueios no ciclo.** Pedidos ao terminal e escritas SQLite síncronas. Em v3.1, cada ciclo de 5 s fazia 15 chamadas de barras mesmo sem barra nova.

**Cobertura mais baixa (medição de 2026-11-01).** `panel/metrics.py` 68%, `panel/app.py` 69%, `team/agents/metalabel.py` 78%, `teambridge.py` 78%. Os valores de `db.py` e `registry.py` nessa medição não são fiáveis, porque os ficheiros foram editados enquanto o relatório corria.

### Tamanho depois da v4

| Medida | v3.1 | v4 |
|---|---:|---:|
| Linhas de código de produção | 18 415 | 23 640 (+28%) |
| Módulos | 97 | 134 |
| `bot.py` | 2091 | 1974 |
| Ciclos de importação | 0 | 0 |
| Importações de `MetaTrader5` | 1, no kernel | 1, no adaptador |
| Pontos do kernel que tocam o terminal | 95 | 0 (só portas) |
| Testes | 986 | 1263 (1261 passam, 2 ignorados) |
| Cobertura do núcleo e da execução | não medida à parte | 93% no conjunto; `execution.py` 96% |

**O princípio 7 ("menos peças, mais fortes") foi cumprido só em parte.** O número de linhas subiu 28%. A execução correcta, a análise de custos, o rigor estatístico, o registo de eventos e o governo são código novo que a v3.1 simplesmente não tinha, e não havia dados para justificar a remoção de agentes. O que baixou foi o que decide por omissão: a equipa não decide sem provar valor, o conselheiro não revê trades, e M5 saiu do universo. O orçamento de complexidade é o mecanismo para cortar mais quando houver dados reais.

## 3. Perfil de desempenho

Um ciclo completo, com `cProfile`, em 400 barras (os tempos estão inflacionados pelo perfilador, a ordem é o que importa).

| Etapa | Tempo acumulado | Observação |
|---|---:|---|
| `run_once` | 277 s | total |
| `_process_symbol` | 155 s | por símbolo e período |
| `arms.on_bar` | 107 s | todos os braços em todas as barras |
| `strategies.signals` | 92 s | 34 020 chamadas, recalculavam toda a história |
| `_housekeeping` | 85 s | em simulação corre em todas as barras |
| `insert_events` | 49 s | escrita da equipa em lotes pequenos |
| `call_many` | 31 s | agentes em paralelo |
| `_write_health` | 20 s | JSON indentado, codificador em Python |
| `arms.refresh` | 19 s | evidência |
| `indicators.ema` | 19 s | ciclo Python sobre toda a série |

Pontos quentes encontrados: indicadores recalculados sobre centenas de barras por braço; `set_kv` de cada livro sombra em todas as barras (cerca de 12 000 escritas em 120 barras); `copy_rates` em todos os ciclos; eventos gravados um a um em pequenos grupos. Medições antes e depois em `PERFORMANCE.md`.

Ciclo de optimização: 5,6 s para 12 estratégias, 8 ensaios e 9000 barras. Não é um ponto crítico.

## 4. Auditoria de execução

Lista oficial verificada em 2026-11-01 em mql5.com: 41 retcodes numéricos (10004 a 10046, sem 10005 nem 10037). Cada um tem agora uma classificação, e um teste garante que nenhum fica de fora.

| Verificação | v3.1 | v4 |
|---|---|---|
| 10009 e 10008 aceites como sucesso | sim, mas 10008 era tratado como preenchido | 10008 só é preenchido depois de confirmado no corretor |
| Parcial 10010 | contava como sucesso sem registo do resto | `PARTIALLY_FILLED` com o volume real |
| Timeout 10012, ligação 10031, erro 10011 | "não executada" | `UNCERTAIN`, reconciliado |
| `None` ou excepção de `order_send` | `None`: não executada; excepção: propagava | `UNCERTAIN`, o adaptador nunca lança |
| Reenvio pode duplicar | só em requote, e sem verificar antes | verifica sempre antes de reenviar; entradas incertas nunca são reenviadas |
| SL e TP verificados depois do preenchimento | só na limpeza, até 60 s depois | imediatamente; anexa, ou fecha se não conseguir |
| Gravação antes do envio | não existia | `INTENT` e `SUBMITTED` em disco antes de enviar |
| Reconciliação | só no arranque | a cada ciclo para ordens abertas e periódica para posições |

## 5. Relatório de realidade

**Não foi possível.** Não existe pasta `state/` nem qualquer base de dados de conta neste repositório, e nesta máquina não há terminal MT5. Não há preenchimentos reais para comparar com sombra ou backtest, e inventar números seria o contrário do objectivo deste trabalho.

A partir de agora a comparação é automática. Depois de operar:

```cmd
.venv\Scripts\python mt5_bot.py --truth
```

## 6. Manter, simplificar ou remover

"Valor medido" está vazio em todas as linhas porque não há dados reais. Custo em linhas de código da v3.1.

| Componente | Valor esperado | Custo | Decisão |
|---|---|---|---|
| Dina, Escriturária de Dados | bloqueia entradas com dados maus | baixo | **manter** no serviço Dados |
| Rui, Detector de Regime | contexto de regime | médio | **manter**; só bloqueia com evidência negativa |
| Sara, Sessões | reduz em horas pouco líquidas | baixo | **manter** |
| Nuno, Notícias | janelas de bloqueio | baixo | **manter** |
| Carla, Correlação | evita risco duplicado | baixo | **simplificar**: o limite passa também pelo portfolio |
| Tomás, Mara, Bruno, Mateus (votantes) | desconhecido; provavelmente correlacionado com os braços | médio, corre em todas as barras | **simplificar**: não decidem por omissão, ablação em curso |
| Lia, Meta-etiquetadora | filtrar sinais fracos | 166 linhas | **manter em avaliação** 90 dias; abstém-se sem dados |
| Rita, Gestora de Risco | essencial | baixo | **manter**, veto final |
| Gil, Guardião | essencial | baixo | **manter**, independente |
| Edu, Execução | essencial | a lógica passou para `execution.py` | **simplificar** para papel de apresentação |
| Paula, Gestão de Posições | só reduz risco | médio | **manter** |
| Quim, Vera (investigação) | essencial | médio | **manter**; nova maquinaria em `rigor.py` |
| Pedro, Desempenho | pesos e ablação | médio | **manter** |
| Rosa, Repórter | relatórios | baixo | **manter** |
| Olavo, Operações | saúde | baixo | **manter** |
| Ciro, Claude | relatórios e explicações | 900 linhas | **simplificar**: sem decidir trades por omissão |
| Flávio, Doutor de Fluxo | desbloquear filtros mortos | 232 linhas | **manter, limitado**: 3 alívios por semana, com prova contrafactual |
| Isabel, CIO | conduz o protocolo | 385 linhas | **manter** |
| `ema_cross`, `donchian` e variantes `volfilter` | tendência | baixo | **manter em avaliação** |
| `momentum_pullback`, `vol_contraction` | tendência e volatilidade | baixo | **manter em avaliação** |
| `session_breakout`, `ny_breakout` | sessões | baixo | **manter em avaliação** |
| `rsi_reversion`, `bollinger`, `range_fade`, `vwap_reversion` | reversão | baixo | **simplificar**: só em regime lateral |
| `ts_momentum`, `asian_range` | agenda de investigação | novo | **acrescentados**, sujeitos às mesmas provas |
| Todos os braços | | | **removidos para investigação** quando falham DSR, PBO ou custo |
| Painel: Escritório e funil | explicação visual | médio | **manter** |
| Painel: 10 dashboards | consulta | médio | **manter**; acrescentados Verdade e Máquina do tempo |
| M5 no universo por omissão | custo elevado face à vantagem | alto em CPU | **removido da omissão**, continua elegível |
| Terminal simulado | duplicava lógica de preenchimento | 287 linhas | **removido** |
| 24 funções sem chamadas (19 herdadas da v3.1, 5 da própria v4) | nenhum | 146 linhas | **removidas** |

## 7. Erros corrigidos

Todos com teste de regressão. Severidade: C crítico, A alto, M médio.

| Sev. | Erro | Teste |
|---|---|---|
| C | Resposta perdida depois de o corretor preencher: posição fora do diário | `test_answer_lost_after_the_broker_filled_does_not_duplicate` |
| C | Sem identidade de ordem: um reinício não podia distinguir uma decisão já enviada | `test_same_decision_after_a_restart_is_blocked` |
| C | Sem gravação antes do envio | `test_terminal_killed_between_write_ahead_and_send` |
| C | SL e TP só verificados até 60 s depois | `test_broker_ignoring_stops_gets_them_attached`, `test_unprotected_window_is_bounded_in_seconds` |
| A | Em netting, o diário guardava o ticket da ordem em vez do da posição | `test_netting_journal_uses_the_position_ticket_not_the_order` |
| A | Em netting, um fecho repetido podia abrir posição contrária | `test_netting_repeat_close_cannot_open_a_reverse_position` |
| A | Parcial sem volume real no diário | `test_the_live_adapter_handles_a_partial_fill_like_the_simulator` |
| A | Posições desconhecidas só eram registadas no arranque | `test_unjournaled_position_is_adopted_logged_and_blocks_entries` |
| M | ADX dependia de barras futuras no limite de comprimento | `test_atr_and_adx_match_batch` |
| M | `_detect_commission` e o disjuntor usavam o relógio do PC, o que impedia replay determinístico | `test_golden_replay_is_byte_identical_across_runs` |
| M | Identificadores aleatórios impediam replay determinístico | `test_golden_replay_is_byte_identical_across_runs` |
| M | O painel `--demo` deixava a simulação órfã ao receber SIGTERM | `test_stopping_the_demo_panel_stops_its_simulator` |

Encontrados e corrigidos durante a construção da v4, antes de chegarem a qualquer conta: a regra de fim de semana lia o relógio do PC em vez do tick do corretor; o stop-out simulado não actuava com margem negativa; um salto do relógio podia provar uma ordem como morta (a carência é agora monotónica).
