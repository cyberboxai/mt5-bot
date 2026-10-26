# Diagnóstico: porque é que o bot observava para sempre e nunca negociava

Este documento explica, com evidência reproduzível, porque é que a versão 3.0 ficava
indefinidamente em "observação" sem abrir uma única operação, o que foi corrigido na versão
3.1 e como confirmar no seu próprio computador.

> Nenhuma destas correcções garante lucro. Elas garantem que o bot **decide** e que cada
> decisão de não negociar fica registada com um motivo. Negociar mais não é o objectivo:
> o objectivo é negociar quando a evidência o justifica, com o risco que a evidência suporta.

## 1. Como medir o funil de decisão (últimas 24 horas)

A partir da versão 3.1 cada barra fechada, cada sinal e cada bloqueio ficam registados na
tabela `funnel_events` da base de dados da conta, com um código de motivo. Para ver o funil:

```
cd /d C:\mt5-bot
.venv\Scripts\activate
python mt5_bot.py --diagnose --hours 24
```

No painel: separador **Porque não estou a negociar?**.

O que o relatório mostra, por esta ordem:

1. barras avaliadas, sinais gerados, sinais bloqueados e ordens executadas;
2. os principais bloqueios, com a parte de cada um e se o Doutor de Fluxo o pode ajustar;
3. o valor de cada filtro: o resultado em sombra dos sinais que esse filtro bloqueou
   ("protegeu" ou "custou oportunidade");
4. braços por nível (T0 a T3), o estado da investigação e os ajustes do Doutor de Fluxo;
5. o que fazer a seguir, em linguagem simples.

Numa instalação 3.0 ainda não migrada, o `--diagnose` reconstrói o que consegue a partir do
registo antigo (ciclos de optimização, portões que falharam, campeões, trades em sombra) e
do `bot.log`.

### Exemplo real (simulação, 6 horas)

```
Funil de decisão das últimas 6 h

  Barras avaliadas ............ 510
  Sinais gerados .............. 289
  Sinais bloqueados ........... 284
  Ordens executadas ........... 5

Principais bloqueios:
   1. ONE_PER_SYMBOL                133   46.8%  já existe uma entrada neste símbolo
   2. THOMPSON_REJECT                88   31.0%  amostra de Thompson abaixo do limiar de exploração
   3. TEAM_VETO                      51   18.0%  veto de um agente de controlo
   ...
Valor dos filtros (resultado virtual dos sinais bloqueados, estimativa):
  THOMPSON_REJECT            n=  63  total virtual   -13.10R  -> protegeu
```

O mesmo comando, sobre o estado da versão 3.0 em simulação, mostrava 180 candidatos
avaliados, **0 aprovados** em todos os portões, 0 campeões e 0 desafiantes.

## 2. Causas, por ordem de importância

### Causa 1 (principal): portões de aceitação praticamente impossíveis de passar

Na 3.0 nenhuma ordem podia nascer sem um "campeão", e um campeão só existia depois de um
candidato passar **todos** os portões walk-forward (Sharpe deflacionado mínimo 0,90 com
penalização por todas as tentativas do ciclo, profit factor 1,3, 100 trades fora da amostra,
60% de janelas lucrativas, bootstrap, vizinhos, drawdown), seguidos de 30 trades **ou** 14
dias em sombra e de um cooldown de 7 dias. Com `NoTrade` como omissão, o resultado normal era
nunca negociar.

**Evidência (reprodução com o código 3.0 e a sua configuração por omissão, 12 meses de
velas M15 sintéticas tipo EURUSD, 7 famílias x 3 sementes, 210 tentativas por ciclo):**

| Mercado | Candidatos | Aprovados | Portões que mais falharam |
|---|---|---|---|
| passeio aleatório (sem vantagem) | 21 | **0** | bootstrap 21, Sharpe deflacionado 21, profit factor 21, pontuação 21, janelas 20 |
| tendência persistente (vantagem real injectada) | 21 | **2** | Sharpe deflacionado 18, bootstrap 13, janelas 12 |

Com uma vantagem verdadeira e forte, só 2 de 21 candidatos passaram, e ainda teriam de
cumprir a sombra e o cooldown. Em dados reais de forex, onde as vantagens são menores, o
caso normal era 0 aprovados. Rejeitar o passeio aleatório estava certo; o problema era não
existir nenhum caminho intermédio com risco pequeno para recolher evidência real.

**Correcção (3.1):** os portões passaram a ser *alertas de qualidade* da investigação, e a
decisão de risco passou para os **braços com níveis de evidência** (ver
[EVIDENCE_AND_RISK.md](EVIDENCE_AND_RISK.md)): cada braço (estratégia + parâmetros + símbolo
+ período) começa em sombra (T0), pode fazer trades de exploração com micro risco pagos por
um orçamento limitado, e sobe de nível só quando a evidência posterior o justifica. Desce
imediatamente quando a evidência piora.

### Causa 2: a optimização de arranque falhava e só voltava a tentar 7 dias depois

**Evidência (reprodução do ciclo de vida 3.0, 24 horas de mercado, `--simulate` sem
campeões colocados):**

```
runs: [{"trigger": "cold start: no optimisation results yet", "status": "failed",
        "notes": "RuntimeError: only 1501 cached bars for EURUSD; not enough history for walk-forward"}]
scheduler_due_after_24h: null
status_line: "OBSERVATION: collecting data; first optimisation pending. No orders until a candidate is promoted."
```

Com histórico insuficiente (por exemplo "Max bars in chart" baixo no MT5), o primeiro ciclo
falhava e o agendador só voltava a tentar na reoptimização semanal. Durante esse tempo o
estado dizia "pending", o que escondia a falha.

**Correcção (3.1):** uma investigação falhada volta a ser tentada ao fim de
`research.retry_minutes` (60 min); o arranque usa uma janela adaptativa
(`research.startup_history_months`, 6 meses, com mínimo de 0,75 meses) e o estado diz
explicitamente "falhou: motivo". Teste de regressão:
`tests/test_autonomy.py::TestScheduler::test_failed_research_is_retried_within_the_hour`.

### Causa 3: evidência nula em contas pequenas

O backtest e a sombra da 3.0 dimensionavam as posições com o capital real da conta. Numa conta
pequena o lote mínimo excedia o risco permitido e o trade virtual era recusado: a sombra não
recolhia evidência nenhuma, e sem evidência nunca havia promoção.

**Correcção (3.1):** a sombra e a investigação usam um capital de referência fixo
(`research.reference_equity`, 10 000, risco 1%): os resultados em múltiplos de R não
dependem do tamanho da conta. Teste: `tests/test_arms.py::TestShadowBook::test_r_multiples_do_not_depend_on_the_account`.

### Causa 4: um único símbolo e um único período

A configuração 3.0 negociava só `EURUSD` em M15, com um campeão por símbolo. Menos
oportunidades, menos evidência, mais tempo à espera.

**Correcção (3.1):** universo de candidatos (até 6 símbolos viáveis escolhidos pelo
explorador de oportunidades) em M5, M15 e H1, com 12 famílias de estratégias, cada
combinação um braço com evidência própria e partilha hierárquica de evidência entre braços
da mesma família.

### Causa 5: filtro de spread em pontos absolutos

`max_spread_points` era um número único em pontos. Em ouro, índices ou pares JPY o mesmo
número tem significados muito diferentes, e o filtro bloqueava ou deixava passar sem
critério.

**Correcção (3.1):** o filtro é relativo ao stop (`universe.max_spread_to_stop`, 25% da
distância ao SL) e a picos face ao spread típico daquela hora (`universe.spread_spike_mult`,
2,5x), com um tecto absoluto opcional por símbolo. Válido para forex, ouro, índices e cripto.

### Causa 6: a linha de estado escondia o motivo

"OBSERVATION: ... first optimisation pending" aparecia tanto quando a optimização estava a
correr como quando tinha falhado; "NO TRADE" não dizia qual portão falhou.

**Correcção (3.1):** a linha de estado é gerada a partir do funil, em português, e nomeia o
bloqueio principal das últimas 6 horas. Cada bloqueio tem um código estável (por exemplo
`OUTSIDE_HOURS`, `SPREAD_LIMIT`, `MIN_LOT_EXCEEDS_RISK`, `EXPLORATION_BUDGET_SPENT`).

### Causa 7 (condicional): risco real de 0,25% e lote mínimo

Em contas reais pequenas, 0,25% do capital é muitas vezes menos do que o lote mínimo
arrisca com um stop de 1,5 ATR. A 3.0 recusava (correcto), mas sem dizer quanto capital seria
preciso.

**Correcção (3.1):** mapa de viabilidade por símbolo e período (capital mínimo para cada
nível) no painel e uma excepção consciente e limitada: o lote mínimo é aceite se o risco
resultante ficar abaixo de `capital.affordability_cap_pct` e dentro do orçamento de
exploração; caso contrário fica registado `MIN_LOT_EXCEEDS_RISK` com o valor.

## 3. Defeitos genuínos encontrados e corrigidos durante o trabalho

Cada um tem um teste de regressão.

| Defeito | Efeito | Teste |
|---|---|---|
| Investigação falhada só tentada de novo 7 dias depois | observação indefinida | `test_autonomy.py::TestScheduler::test_failed_research_is_retried_within_the_hour` |
| O funil escrevia `BEGIN/INSERT/COMMIT` como instruções separadas na ligação partilhada | o escritor de eventos do painel entrava a meio da transacção ("cannot start a transaction within a transaction") e perdia eventos | `test_funnel_flow.py::TestFunnel::test_flush_never_interleaves_with_the_office_writer` |
| Um `COMMIT` falhado deixava a ligação dentro de uma transacção | todas as escritas seguintes falhavam | `test_funnel_flow.py::TestFunnel::test_failed_commit_never_leaves_the_connection_in_a_transaction` |
| A equipa media barras H1 com a duração de uma barra M15 | a Escriturária de Dados via todas as barras H1 como atrasadas e vetava **todas** as propostas H1 | `test_team_integration.py::test_team_judges_bars_on_the_proposing_arms_timeframe` |
| Evidência lida com `close_ts > último` | trades fechados no mesmo instante (relógio virtual) perdiam-se para sempre | `test_regime.py::test_trades_closed_at_the_same_instant_are_never_lost` |
| Barras abertas das posições contadas com a chave errada | o stop por tempo nunca disparava | `test_management.py::test_live_position_follows_its_arm_policy` |
| O Doutor de Fluxo alargava o universo sem símbolos disponíveis | ajustes sem efeito, depois revertidos com evidência alheia | corrigido em `Bot._can_add_symbol` (verificado na simulação ponta a ponta) |
| O Doutor de Fluxo relaxava um filtro por um bloqueio raro (1 em 284) | o filtro de spread foi alargado sem motivo | `test_funnel_flow.py::TestFlowDoctor::test_rare_blockers_never_loosen_a_filter` |
| `--diagnose` numa simulação comparava tempo virtual com o relógio do PC | dizia "actualize para 3.1" numa instalação 3.1 | `test_funnel_flow.py::TestDiagnose::test_simulation_is_read_on_its_own_clock_and_quiet_windows_say_so` |
| `python -m panel --demo` com uma pasta de demonstração nova | o painel caía ao abrir o ficheiro de registo | corrigido em `panel/app.py` (a pasta é criada) |
| Teste ponta a ponta: regime adverso só atacava posições reais | a evidência em sombra subia e a despromoção nunca acontecia | `test_e2e.py` (lento) agora passa em cerca de 5 minutos |

## 4. Como reproduzir

Os scripts de reprodução da 3.0 foram corridos sobre uma cópia do código anterior (o
`HEAD` do repositório antes desta mudança):

1. Portões: optimização walk-forward 3.0 em 12 meses de velas sintéticas, passeio aleatório e
   tendência persistente, contagem do portão que rejeita cada candidato (secção 2, causa 1).
2. Ciclo de vida: o bot 3.0 em simulação durante 24 horas de mercado, sem campeões
   colocados, registando o estado, os ciclos de optimização e o agendador (causa 2).

Na 3.1 o mesmo cenário é coberto por testes automáticos:

* `tests/test_e2e.py` (com `--runslow`): arranque a frio, investigação, primeiro trade em
  menos de 48 horas de mercado, promoção a T2 com vantagem verdadeira, despromoção em regime
  adverso;
* `tests/test_evidence_tiers.py`: em 120 mundos sem vantagem, no máximo 3 promoções falsas a
  T2 e nenhuma a T3; vantagens verdadeiras chegam a T3;
* `tests/test_pipeline.py`: cada portão deixa um código de motivo no funil.

Para ver o bot a negociar em minutos sem corretora:

```
python -m panel --demo
```

e abrir http://127.0.0.1:8765/. Na simulação a primeira ordem de exploração aparece poucos
minutos virtuais depois do arranque.

## 5. O que pode continuar a impedir trades (e é intencional)

* mercado fechado, fim de semana ou fora de `trading_hours_server` (hora do servidor);
* orçamento de exploração do dia ou da semana gasto (recomeça sozinho);
* limite de perda diária, piso de protecção de lucros ou disjuntor de drawdown;
* lote mínimo acima do risco permitido para o capital da conta;
* braços cuja evidência ficou negativa (a amostra de Thompson fica abaixo de zero).

Em todos estes casos o `--diagnose` e o painel dizem qual é o motivo.
