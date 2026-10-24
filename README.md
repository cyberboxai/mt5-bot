# Bot de negociação para MetaTrader 5, versão 2 (Python, Windows)

Guia completo de instalação e operação do bot `mt5_bot.py`, que negoceia automaticamente através do terminal MetaTrader 5 (MT5) instalado no seu PC Windows. Foi pensado para a corretora STARTRADER, mas funciona com qualquer corretora MT5.

## Índice

1. [O que é isto e o que mudou na v2](#1-o-que-é-isto-e-o-que-mudou-na-v2)
2. [Aviso de risco (leia primeiro)](#2-aviso-de-risco-leia-primeiro)
3. [Requisitos](#3-requisitos)
4. [Instalar o Python](#4-instalar-o-python)
5. [Instalar o MT5 e entrar numa conta](#5-instalar-o-mt5-e-entrar-numa-conta)
6. [Preparar o MT5 para automação](#6-preparar-o-mt5-para-automação)
7. [Instalar o projecto](#7-instalar-o-projecto)
8. [Selecção de conta: demo ou real](#8-selecção-de-conta-demo-ou-real)
9. [Como funciona o motor adaptativo](#9-como-funciona-o-motor-adaptativo)
10. [Referência completa do config.json](#10-referência-completa-do-configjson)
11. [Primeira execução e fase de observação](#11-primeira-execução-e-fase-de-observação)
12. [Execução sem supervisão](#12-execução-sem-supervisão)
13. [Operações do dia a dia](#13-operações-do-dia-a-dia)
14. [Adicionar uma estratégia e ajustar limites](#14-adicionar-uma-estratégia-e-ajustar-limites)
15. [Credenciais por variáveis de ambiente](#15-credenciais-por-variáveis-de-ambiente)
16. [Backtest e optimização manual](#16-backtest-e-optimização-manual)
17. [Resolução de problemas](#17-resolução-de-problemas)
18. [Executar os testes](#18-executar-os-testes)
19. [Limitações e pressupostos](#19-limitações-e-pressupostos)

---

## 1. O que é isto e o que mudou na v2

O bot liga-se ao terminal MT5 aberto no seu PC (pacote oficial `MetaTrader5`), lê velas fechadas, escolhe sozinho qual estratégia pode negociar e envia ordens a mercado **sempre com stop loss (SL) e take profit (TP)** no servidor da corretora.

O que se manteve da v1: volume calculado pelo risco, SL e TP em todas as ordens, `order_check` antes de cada envio, repetição em caso de requote, kill switch, reconexão automática, registo rotativo `bot.log`, limite de perda diária persistente e testes com um MT5 simulado.

O que mudou na v2:

| Área | v1 | v2 |
|---|---|---|
| Contas | Recusava contas reais (`allow_live`). | Aceita demo **ou** real. Dinheiro real exige uma confirmação explícita, feita uma única vez (secção 8). |
| Estratégia | Uma estratégia fixa (cruzamento de EMA). | Biblioteca de 7 estratégias com parâmetros limitados, optimização walk-forward, validação em sombra (shadow), promoção e despromoção automáticas, e `NoTrade` quando nada demonstra vantagem. |
| Segurança | Limites no `config.json`. | Camada de risco separada com tectos rígidos no código (`mt5bot/limits.py`) que nenhuma configuração ou estratégia ultrapassa, disjuntor de drawdown e comportamentos proibidos testados. |
| Autonomia | Arranque manual. | Supervisor com reinício automático, arranque com o Windows, abertura e reinício do terminal MT5, reconciliação com a corretora, recarga do `config.json`, ficheiro de estado `health.json`, relatórios diários e semanais e notificações Telegram opcionais. |
| Estado | Um ficheiro `bot_state.json`. | Uma pasta por conta em `state\<servidor>_<login>\` com base de dados SQLite (diário de operações, registo de estratégias, decisões). |

Estrutura do projecto:

```
mt5_bot.py              ponto de entrada (python mt5_bot.py ...)
supervisor.py           supervisor que mantém o bot a correr
config.json             configuração
mt5bot\                 código: risk.py, limits.py, engine.py, optimizer.py, backtest.py, bot.py, ...
mt5bot\strategies\      biblioteca de estratégias (um ficheiro por estratégia)
tests\                  testes automáticos (pytest)
start_bot.bat           arranca só o bot
start_supervisor.bat    arranca o supervisor (recomendado para uso contínuo)
install_autostart.bat   regista o arranque automático no Agendador de Tarefas
uninstall_autostart.bat remove esse registo
```

---

## 2. Aviso de risco (leia primeiro)

> * **Nenhum software garante lucro.** Este bot também não. Pode perder dinheiro, incluindo todo o capital da conta.
> * **A maioria dos investidores particulares que negoceiam CFD e forex perde dinheiro.** As próprias corretoras publicam percentagens de contas perdedoras, normalmente entre 70% e 80%.
> * **Sistemas adaptativos podem sobre-optimizar (overfitting):** encontram padrões no passado que são apenas acaso e que não se repetem. A v2 foi desenhada para combater isso (validação fora da amostra, penalização por número de tentativas, validação em sombra), mas nenhuma técnica elimina este risco.
> * **O objectivo do bot é maximizar a expectativa ajustada ao risco e aos custos, fora da amostra, e preservar o capital acima de tudo.** Quando nenhuma estratégia demonstra vantagem, a decisão correcta é **não negociar**. O bot pode ficar dias, semanas ou meses sem abrir operações. **Isso é intencional, não é uma avaria.**
> * Resultados de backtest são estimativas. A execução real tem deslizamento (slippage), spreads variáveis, saltos de preço (gaps) e falhas de internet, energia ou da corretora.
> * Comece numa conta **demo** durante várias semanas. Passe a real só com capital pequeno que pode perder na totalidade. Este software é fornecido sem qualquer garantia.

---

## 3. Requisitos

| Requisito | Detalhe |
|---|---|
| Sistema operativo | Windows 10 ou 11, 64 bits. O pacote `MetaTrader5` só funciona em Windows. |
| Python | 3.10, 3.11 ou 3.12, **64 bits**. Recomendado: 3.12. |
| MetaTrader 5 | O terminal MT5 da STARTRADER, transferido do site oficial ou da Área de Cliente da corretora. |
| Conta | Uma conta DEMO para começar (login, palavra-passe e nome do servidor). |
| PC | Ligado em permanência, com o terminal MT5 aberto e com sessão iniciada. 4 GB de RAM livres e alguns GB de disco para o histórico. |

Verifique o Python (depois de o instalar, secção 4) numa janela `cmd`:

1. Liste as versões instaladas:
   ```
   py -0p
   ```
   **Sucesso:** aparece uma linha como `-V:3.12 *        C:\Users\SeuNome\AppData\Local\Programs\Python\Python312\python.exe`. Se aparecer `-32` no fim da versão, essa instalação é de 32 bits e não serve.
2. Confirme os 64 bits:
   ```
   python -c "import struct; print(struct.calcsize('P')*8)"
   ```
   **Sucesso:** imprime `64`.

---

## 4. Instalar o Python

1. Abra https://www.python.org/downloads/windows/ e, em "Stable Releases", escolha a versão 3.12 mais recente e clique em **Windows installer (64-bit)**.
2. Execute o instalador. No primeiro ecrã, **marque "Add python.exe to PATH"**.
3. Clique em **Install Now** e aguarde "Setup was successful". Clique em **Close**.
4. Abra uma **nova** janela `cmd` (tecla Windows, escreva `cmd`, Enter).
5. Verifique:
   ```
   py --version
   python --version
   ```
   **Sucesso:** ambos imprimem `Python 3.12.x`. Se `python` abrir a Microsoft Store, vá a **Definições > Aplicações > Definições avançadas de aplicações > Aliases de execução de aplicações**, desligue `python.exe` e `python3.exe`, e reabra o `cmd`.

---

## 5. Instalar o MT5 e entrar numa conta

1. Transfira o MetaTrader 5 do site oficial da STARTRADER ou da sua Área de Cliente. Execute o instalador com as opções por omissão.
   **Sucesso:** o MT5 abre e mostra gráficos.
2. Crie uma conta demo na Área de Cliente ou no MT5 em **File > Open an Account**. Guarde o **login**, a **palavra-passe** e o **nome do servidor**.
3. No MT5 abra **File > Login to Trade Account**, preencha Login, Password (a de negociação, não a de investidor) e Server, marque **Save password** e clique em **OK**.
4. Confirme a ligação: no canto inferior direito aparecem barras verdes e o ping em milissegundos; a barra de título mostra o número da conta e `Demo Account`.

> **Dica:** para seguir este guia à letra, ponha o MT5 em inglês em **View > Languages > English** e reinicie-o.

---

## 6. Preparar o MT5 para automação

1. **Tools > Options**, separador **Expert Advisors**:
   * marque **Allow algorithmic trading**;
   * se existir **Disable algorithmic trading via external Python API**, **desmarque-a**;
   * recomendado: desmarque **Disable algorithmic trading when the account has been changed**.

   Clique em **OK**.
2. Clique no botão **Algo Trading** (Ctrl+E) até ficar **verde**.
3. **Tools > Options > Charts > Max bars in chart**: escolha **Unlimited** (ou pelo menos 100000). O optimizador pede cerca de 12 meses de velas; com um valor baixo recebe menos histórico.
4. **View > Market Watch** (Ctrl+M), botão direito, **Show All**. Anote o nome exacto do símbolo, por exemplo `EURUSD` ou `EURUSD.m`. O bot detecta sufixos sozinho (secção 17), mas confirmar não custa.
5. O cabeçalho do Market Watch mostra a **hora do servidor**. O bot detecta automaticamente a diferença para UTC a partir dos ticks; `trading_hours_server` usa a hora do servidor.

---

## 7. Instalar o projecto

1. Crie a pasta e entre nela:
   ```
   mkdir C:\mt5-bot
   cd /d C:\mt5-bot
   ```
2. Copie para `C:\mt5-bot` todos os ficheiros e pastas do projecto (`mt5_bot.py`, `supervisor.py`, `config.json`, a pasta `mt5bot`, a pasta `tests`, os ficheiros `.bat` e `.ps1`, `requirements.txt`, `requirements-dev.txt`, `README.md`).
3. Crie e active o ambiente virtual:
   ```
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   ```
   **Sucesso:** o cursor passa a `(.venv) C:\mt5-bot>`.
4. Instale as dependências (só `MetaTrader5` e `numpy`; o resto é biblioteca padrão do Python):
   ```
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
   **Sucesso:** a última linha diz `Successfully installed MetaTrader5-... numpy-...`.
5. Com o MT5 aberto e com sessão iniciada, teste a ligação:
   ```
   python -c "import MetaTrader5 as m; print(m.initialize(), m.account_info())"
   ```
   **Sucesso:** imprime `True AccountInfo(login=..., trade_mode=0, ...)`. `trade_mode=0` é demo, `2` é real.

---

## 8. Selecção de conta: demo ou real

O bot negoceia na conta que estiver com sessão iniciada no terminal (ou na conta indicada pelas variáveis de ambiente da secção 15). Ao arrancar mostra uma faixa com o número da conta (mascarado no registo, por exemplo `***321`), o servidor, `DEMO` ou `REAL MONEY`, o capital, a moeda, a alavancagem e o tipo de conta (hedging ou netting).

### 8.1 A chave `account_mode`

| Valor | Efeito |
|---|---|
| `"any"` (omissão) | Aceita a conta que estiver no terminal, demo ou real. |
| `"demo_only"` | Recusa contas reais (sai com o código 3). |
| `"real_only"` | Recusa contas demo (sai com o código 3). |

### 8.2 Confirmar dinheiro real (uma única vez, de propósito)

Numa conta real, o bot só negoceia depois de uma confirmação explícita. Sem ela, mostra esta mensagem e sai com o código 3:

```
This terminal is logged in to a REAL MONEY account. The bot only trades real money after one deliberate confirmation...
```

Escolha **uma** das formas:

1. Na linha de comandos (vale para essa execução):
   ```
   cd /d C:\mt5-bot
   .venv\Scripts\activate
   python mt5_bot.py --confirm-real-money
   ```
2. Ou de forma permanente no `config.json` (recomendado para arranque automático):
   ```
   "real_money_confirmed": true
   ```

Na primeira execução numa conta real aparece uma contagem decrescente de 10 segundos (`REAL MONEY trading starts in 10 s`). Ctrl+C durante a contagem cancela sem negociar. Para reinícios sem supervisão use `--no-countdown` (o supervisor já o acrescenta sozinho nos reinícios).

**Porque é um passo deliberado:** uma predefinição escondida ou uma confirmação automática tornaria fácil arrancar em dinheiro real por engano (por exemplo depois de trocar de conta no terminal). Uma confirmação explícita, feita por si, é a protecção mais simples contra esse erro, e depois dela o bot não pede mais nada.

### 8.3 Risco mais baixo em contas reais

| Chave | Demo | Real | Tecto rígido no código |
|---|---|---|---|
| Risco por operação | `risk_per_trade_pct_demo` = 0.5% | `risk_per_trade_pct_real` = 0.25% | 2.0% |
| Perda diária máxima | `max_daily_loss_pct_demo` = 2.0% | `max_daily_loss_pct_real` = 1.5% | 5.0% |

### 8.4 Estado separado por conta

Cada conta tem a sua pasta `state\<servidor>_<login>\`, por exemplo `state\STARTRADER-Demo_12345678\`. Lá ficam:

* `registry.sqlite`: diário de operações (reais e em sombra), estratégias, resultados de optimização, decisões e capital;
* `risk_state.json`: capital do início do dia e pico de capital;
* `RESUME_REQUIRED.txt`: só existe se o disjuntor de drawdown disparou;
* `optimizer.log`: registo das optimizações em segundo plano.

O histórico da demo **nunca** influencia as decisões da conta real e vice-versa. Os relatórios também ficam separados em `reports\<servidor>_<login>\`.

### 8.5 Contas hedging e netting

O bot detecta o tipo de conta (`margin_mode`):

* **hedging:** pode haver várias posições no mesmo símbolo; o bot fecha a sua posição pelo número (ticket);
* **netting:** há uma única posição por símbolo; o bot fecha com uma ordem oposta do mesmo volume, altera SL/TP com `TRADE_ACTION_SLTP` e **não abre** se já existir uma posição de outra origem (manual ou outro robô) nesse símbolo.

---

## 9. Como funciona o motor adaptativo

Em linguagem simples: o bot tem uma biblioteca de estratégias revistas, testa-as em dados que não usou para as afinar, põe as melhores a "negociar a fingir" (em sombra) com dados ao vivo e só deixa uma negociar a sério quando passou todos os testes. Se essa estratégia piorar, é afastada de imediato. Se nenhuma passar, o bot fica de fora.

```
 Biblioteca de estratégias (código revisto, parâmetros com limites)
   ema_cross | donchian | rsi_reversion | bollinger | session_breakout
   volfilter_ema_cross | volfilter_donchian | NoTrade
            |
            v
 Optimizador (processo separado, prioridade baixa, semanal ao fim de semana)
   amostra aleatória de parâmetros dentro dos limites
            |
            v
 Walk-forward: treino 3 meses -> teste 1 mês nunca visto -> avança 1 mês ...
            |
            v
 Portões de aceitação (todos obrigatórios, fora da amostra, com custos)
   >= 100 operações | expectativa > 0 e limite inferior bootstrap 90% > 0
   profit factor >= 1.3 | drawdown <= 12R | lucro em >= 60% das janelas
   Sharpe deflacionado >= 0.90 (penaliza o número de tentativas)
   vizinhos dos parâmetros também aceitáveis (sem picos isolados)
            |
     passou? ---- não ----> NoTrade (não negociar)
            | sim
            v
 Desafiantes em SOMBRA: sinais e execuções virtuais com dados ao vivo, sem ordens
            |
            v
 Promoção a CAMPEÃO se: portões + >= 30 operações em sombra (ou >= 14 dias com >= 10)
   + sombra positiva e coerente com o backtest + supera o campeão em >= 15%
   + passaram >= 7 dias desde a última troca
            |
            v
 CAMPEÃO: a única estratégia que envia ordens reais (sempre via camada de risco)
            |
 degradação? (drawdown acima do orçamento, expectativa recente claramente
              negativa, sequência de perdas improvável)
            | sim
            v
 DESPROMOÇÃO imediata -> próximo candidato validado ou NoTrade
   -> nova optimização -> notificação
```

Pontos importantes:

* **Camada de risco:** as estratégias só propõem uma direcção (compra ou venda) e uma volatilidade (ATR). O módulo `mt5bot/risk.py` decide se a operação pode abrir e com que volume. O mesmo código é usado no backtest, na sombra e em real, por isso os três não divergem.
* **Comportamentos proibidos (no código e nos testes):** martingale, aumentar o volume depois de uma perda, juntar posições em perda (averaging down), grelhas, ordens sem SL e TP, remover ou alargar um SL, negociar com spread ou deslizamento acima do limite, negociar em janelas de notícias (se o filtro estiver activo).
* **Nada de código gerado em tempo real:** o motor só escolhe entre estratégias e valores de parâmetros definidos no código. Não usa `eval`, `exec` nem descarrega código.
* **Trocas não abandonam posições:** uma posição aberta mantém o seu SL e TP e continua gerida pela estratégia que a abriu (incluindo o fecho por sinal contrário). A nova campeã só faz novas entradas.
* **Arranque a frio:** sem campeã, o bot fica em **modo de observação**: recolhe dados, optimiza, negoceia em sombra e só envia ordens depois da primeira promoção.
* **Custos realistas no backtest:** entrada na abertura da vela seguinte, spread de cada vela, deslizamento configurável, comissão por lote, swap negativo por noite, e, quando o SL e o TP são tocados na mesma vela, assume-se que o SL foi atingido primeiro (pessimista).

---

## 10. Referência completa do config.json

Regras do formato JSON: texto entre aspas duplas, `true`/`false`/`null` em minúsculas, sem vírgula depois do último item, e em caminhos do Windows cada `\` escreve-se `\\`. O bot valida tudo ao arrancar e em cada recarga; chaves desconhecidas são recusadas com uma mensagem clara. As chaves da v1 que foram removidas (`allow_live`, `risk_per_trade_pct`, `max_daily_loss_pct`, `fast_ema`, `slow_ema`, `atr_period`, `sl_atr_mult`, `tp_atr_mult`, `state_file`) dão um erro que diz o que as substitui.

O `config.json` que acompanha o projecto lista as chaves mais usadas; as restantes usam o valor por omissão desta tabela. "Tecto" é o máximo rígido do código: o bot recusa valores acima dele.

### 10.1 Mercado e conta

| Chave | Significado | Omissão | Intervalo seguro | Tecto |
|---|---|---|---|---|
| `symbols` | Símbolos a negociar (o bot detecta sufixos como `.m`). | `["EURUSD"]` | 1 a 5 símbolos líquidos | |
| `timeframe` | Período das velas: `M1`, `M5`, `M15`, `M30`, `H1`, `H4`. | `"M15"` | `M15` a `H1` | |
| `bars` | Velas fechadas enviadas às estratégias em tempo real. | `1000` | 1000 a 2000 | 20000 |
| `history_months` | Meses de histórico para a optimização. | `12` | 12 a 24 | 60 |
| `magic` | Número que identifica as ordens deste bot. Só mexe em posições com este número. | `510001` | Único por instância | |
| `account_mode` | `any`, `demo_only` ou `real_only` (secção 8). | `"any"` | | |
| `real_money_confirmed` | Confirmação permanente para contas reais. | `false` | | |

### 10.2 Risco (camada de risco)

| Chave | Significado | Omissão | Intervalo seguro | Tecto |
|---|---|---|---|---|
| `risk_per_trade_pct_demo` | % do capital arriscada até ao SL (demo), comissão incluída. | `0.5` | 0.25 a 1 | 2.0 |
| `risk_per_trade_pct_real` | Idem, contas reais. | `0.25` | 0.1 a 0.5 | 2.0 |
| `max_daily_loss_pct_demo` | Perda diária máxima (dia do servidor). Ao atingir: fecha posições do bot e pára até ao dia seguinte. | `2.0` | 1 a 3 | 5.0 |
| `max_daily_loss_pct_real` | Idem, contas reais. | `1.5` | 1 a 2 | 5.0 |
| `max_open_positions_total` | Máximo de posições do bot em simultâneo. | `3` | 1 a 3 | 5 |
| `max_total_open_risk_pct` | Soma do risco até ao SL de todas as posições abertas. | `2.0` | 1 a 2 | 4.0 |
| `max_symbol_exposure_x` | Exposição nocional por símbolo, em múltiplos do capital. | `5.0` | 2 a 5 | 10.0 |
| `max_drawdown_from_peak_pct` | Disjuntor: queda do capital face ao pico que fecha tudo e exige intervenção humana (secção 13). | `12.0` | 8 a 15 | 20.0 |

### 10.3 Execução, filtros e custos

| Chave | Significado | Omissão | Intervalo seguro |
|---|---|---|---|
| `max_spread_points` | Spread máximo para abrir (10 pontos = 1 pip em EURUSD). | `20` | 1,5 a 3 vezes o spread típico |
| `deviation_points` | Desvio de preço aceite na execução. Entra no cálculo do volume. | `10` | 5 a 30 |
| `max_slippage_points` | Deslizamento acima do qual novas entradas nesse símbolo ficam em pausa. | `20` | 10 a 30 |
| `slippage_pause_minutes` | Duração dessa pausa. | `60` | 30 a 240 |
| `trading_hours_server` | Janela `[início, fim)` em horas do servidor para novas entradas. `[0, 24]` = sempre. | `[7, 20]` | Evite o rollover (cerca das 00:00 do servidor) |
| `close_on_opposite` | Um sinal contrário da estratégia que abriu a posição fecha-a. | `true` | |
| `commission_per_lot` | Comissão por lote, ida e volta, na moeda da conta. `null` = detectar no histórico de negócios. | `null` | |
| `commission_fallback_per_lot` | Valor usado se não houver histórico para detectar (pessimista). | `7.0` | 0 em contas só com spread |
| `backtest_slippage_points` | Deslizamento assumido no backtest e na sombra. | `2.0` | 1 a 5 |
| `news_filter_enabled` | Bloqueia entradas perto de notícias do ficheiro `news_file`. | `false` | |
| `news_file` | CSV com colunas `time_utc,currency,impact,title`. | `"news.csv"` | |
| `news_minutes_before` / `news_minutes_after` | Janela de bloqueio à volta de cada notícia. | `30` / `30` | 15 a 60 |

### 10.4 Motor adaptativo

| Chave | Significado | Omissão | Intervalo seguro |
|---|---|---|---|
| `enabled_strategies` | Lista de estratégias activas. `null` = todas. | `null` | |
| `trials_per_strategy` | Conjuntos de parâmetros testados por estratégia. | `30` | 20 a 60 |
| `max_trials_per_cycle` | Limite total de tentativas por ciclo (entra na penalização). | `300` | 100 a 500 |
| `optimizer_workers` | Processos paralelos do optimizador. | `1` | 1 a (núcleos - 1) |
| `optimizer_seed` | Semente: o mesmo histórico dá sempre o mesmo resultado. | `12345` | |
| `wf_train_months` / `wf_test_months` / `wf_step_months` | Janelas walk-forward. | `3` / `1` / `1` | |
| `min_oos_trades` | Operações mínimas fora da amostra. | `100` | 100 a 300 |
| `min_profit_factor` | Profit factor mínimo fora da amostra. | `1.3` | 1.2 a 1.6 |
| `max_oos_drawdown_r` | Drawdown máximo fora da amostra, em R (1R = risco de uma operação). | `12.0` | 8 a 15 |
| `min_window_win_ratio` | Fracção mínima de janelas de teste com lucro. | `0.6` | 0.55 a 0.75 |
| `bootstrap_confidence` / `bootstrap_samples` | Intervalo bootstrap da expectativa por operação. | `0.90` / `2000` | |
| `min_deflated_sharpe` | Probabilidade mínima de o Sharpe ser melhor do que o melhor acaso esperado para o número de tentativas. **O portão mais exigente.** | `0.90` | 0.80 a 0.95 |
| `min_neighbor_pass_ratio` | Fracção de parâmetros vizinhos que também têm de ser aceitáveis. | `0.6` | 0.5 a 0.8 |
| `max_challengers` | Desafiantes em sombra por símbolo. | `3` | 2 a 5 |
| `min_shadow_trades` | Operações em sombra para poder ser promovido. | `30` | 30 a 60 |
| `min_shadow_days` / `min_shadow_trades_floor` | Alternativa: este número de dias com pelo menos este número de operações. | `14` / `10` | |
| `promotion_margin` | Histerese: quanto o desafiante tem de superar o campeão. | `0.15` | 0.1 a 0.3 |
| `switch_cooldown_days` | Dias mínimos entre trocas de campeão. | `7` | 7 a 14 |
| `bootstrap_days` | Dias mínimos de observação antes da primeira promoção (também `--bootstrap-days`). | `null` | |
| `degrade_window_trades` / `degrade_min_trades` / `degrade_ucb_z` | Despromove se o limite superior da expectativa das últimas operações for negativo. | `30` / `10` / `1.645` | |
| `degrade_dd_multiple` / `degrade_max_dd_r` | Orçamento de drawdown: o menor de (múltiplo x drawdown fora da amostra) e este limite em R. | `2.0` / `10.0` | |
| `degrade_streak_alpha` | Despromove se uma sequência de perdas tiver probabilidade abaixo deste valor. | `0.01` | 0.005 a 0.02 |
| `reoptimize_every_days` | Ciclo completo de optimização (feito ao fim de semana). | `7` | 7 |

### 10.5 Operação

| Chave | Significado | Omissão |
|---|---|---|
| `dry_run` | Faz tudo, incluindo `order_check`, mas nunca envia ordens. Igual a `--dry-run`. | `false` |
| `poll_seconds` | Intervalo entre ciclos com o mercado activo. | `5` |
| `closed_market_poll_seconds` | Intervalo com o mercado fechado (fim de semana, feriados). | `60` |
| `heartbeat_minutes` | Intervalo das mensagens `Heartbeat` e da linha de estado no registo. | `30` |
| `stale_tick_minutes` | Minutos sem ticks para considerar o mercado parado. | `5` |
| `terminal_restart_enabled` / `terminal_freeze_minutes` | Reinicia o terminal se não houver ticks durante este tempo em horário de mercado (só se todas as posições tiverem SL e TP). | `true` / `10` |
| `holidays` | Feriados (datas do servidor `"AAAA-MM-DD"`) em que o mercado não abre. | `[]` |
| `config_reload_seconds` | De quanto em quanto tempo o `config.json` é relido. | `60` |
| `telegram_enabled` | Notificações Telegram (secção 12.6). | `false` |
| `kill_switch_file` | Ficheiro de paragem de emergência. | `"KILL"` |
| `state_root` / `reports_dir` / `health_file` / `log_file` | Pastas e ficheiros de estado, relatórios, saúde e registo. | `"state"` / `"reports"` / `"health.json"` / `"bot.log"` |
| `mt5_path` | Caminho do `terminal64.exe`. `null` = usar o terminal aberto ou procurar em Program Files (prefere pastas com "STARTRADER"). | `null` |
| `mt5_init_timeout_ms` | Tempo de espera do arranque do terminal. | `60000` |

**Recarga automática:** o bot relê o `config.json` a cada minuto. Se o novo ficheiro for inválido, mantém a configuração anterior e escreve o motivo no `bot.log`. As chaves de segurança só podem ser **apertadas** em funcionamento (por exemplo baixar o risco); para as alargar, ou para mudar `symbols`, `timeframe`, `magic`, `account_mode`, `real_money_confirmed`, pastas ou `mt5_path`, reinicie o bot. Os tectos de `mt5bot/limits.py` só mudam alterando o código.

---

## 11. Primeira execução e fase de observação

### 11.1 Arrancar em demo, primeiro em dry-run

```
cd /d C:\mt5-bot
.venv\Scripts\activate
python mt5_bot.py --dry-run
```

**Sucesso:** aparecem linhas como estas (valores ilustrativos):

```
INFO     ========================================================================
INFO       ACCOUNT ***321 on STARTRADER-Demo: DEMO
INFO       equity 10000.00 USD, leverage 1:500, hedging account
INFO       risk per trade 0.50%, daily loss limit 2.00%, drawdown breaker 12.0% from peak
INFO       mode: DRY RUN (no orders will be sent)
INFO     Reconciliation: 0 bot position(s) open, 0 recovered into the journal, 0 repaired, 0 pending order(s)
INFO     EURUSD: cached 26112 bars of history for optimisation
INFO     Optimisation process started (low priority): cold start: no optimisation results yet
INFO     EURUSD: OBSERVATION: collecting data; first optimisation running. No orders until a candidate is promoted.
```

### 11.2 O que esperar nos primeiros dias

| Quando | O que acontece | Linha de estado |
|---|---|---|
| Minuto 0 | O bot guarda 12 meses de velas e lança a primeira optimização em segundo plano (1 a 3 minutos por símbolo num PC normal). | `OBSERVATION: collecting data; first optimisation running` |
| Minutos depois | Se nenhuma estratégia passou os portões, fica em `NoTrade` até ao próximo ciclo semanal. | `NO TRADE: no candidate passed the gates in run N; standing aside by design` |
| Dias 1 a 14+ | Se houver sobreviventes, negoceiam em sombra. | `OBSERVATION: best challenger donchian(...): shadow 12/30 trades, 5.2/14 days, mean +0.210R` |
| Primeira promoção | Com 30 operações em sombra (ou 14 dias com pelo menos 10) coerentes com o backtest. Normalmente **2 a 6 semanas**, e **pode nunca acontecer**. | `champion donchian(...) trading` |

É normal o bot não abrir nenhuma operação durante semanas. Com `--bootstrap-days 21` (ou `"bootstrap_days": 21`) obriga a pelo menos 21 dias de observação antes da primeira promoção.

### 11.3 Caminho recomendado

1. Uma semana em **demo com `--dry-run`**: confirme que não há erros e veja as decisões em `--status` e `--history`.
2. **Várias semanas em demo sem dry-run** (de preferência até ver uma promoção e algumas operações): compare o comportamento real com o que o `--history` justificou.
3. Só depois, **conta real com capital pequeno**, com `real_money_confirmed` (secção 8.2). O estado da conta real começa do zero (pastas separadas): volta a passar pela observação.

### 11.4 Ler o estado

* Estado actual (lê o `health.json`, actualizado a cada minuto):
  ```
  python mt5_bot.py --status
  ```
  **Sucesso:** mostra conta, capital, pico, drawdown, posições, paragens activas, campeão por símbolo, linha de estado e progresso de cada desafiante. Se o ficheiro tiver mais de 5 minutos, avisa que o bot pode não estar a correr.
* Registo de decisões (optimizações, promoções, despromoções, rollbacks), com motivo e métricas:
  ```
  python mt5_bot.py --history
  ```
  Exemplo:
  ```
  #7     2026-11-09 06:10 UTC  EURUSD     PROMOTE      NoTrade -> donchian(channel=60, sl_mult=2.0, tp_mult=3.5)
         reason: gates passed (OOS score 1.234); 31 shadow trades over 16.0 days, mean +0.280R ...
  ```
* Relatórios diários e semanais em `reports\<servidor>_<login>\`: `daily_AAAA-MM-DD.txt` (resumo: operações, taxa de acerto, expectativa, profit factor, drawdown, campeão e porquê), `..._trades.csv` e `..._equity.csv`. Os semanais são gerados às segundas-feiras.
* Registo completo: `bot.log` (rotativo, 2 MB x 5). Útil:
  ```
  findstr /C:"PROMOTED" /C:"DEMOTED" /C:"CRITICAL" /C:"ERROR" bot.log
  powershell -Command "Get-Content bot.log -Wait -Tail 30"
  ```

---

## 12. Execução sem supervisão

### 12.1 O supervisor

O supervisor arranca o bot como processo filho e reinicia-o se ele terminar por erro, com espera crescente (15 s, 30 s, 60 s... até 5 minutos). Se houver 5 falhas em 30 minutos, pára de propósito (disjuntor de reinícios), notifica e deixa as posições protegidas pelo SL/TP no servidor. Não reinicia depois de saídas deliberadas: código 0 (Ctrl+C, kill switch) e código 3 (regra de segurança, conta não autorizada).

```
cd /d C:\mt5-bot
start_supervisor.bat
```

Para contas reais com arranque automático, use `"real_money_confirmed": true` no `config.json` (ou `start_supervisor.bat --confirm-real-money`). O registo do supervisor fica em `supervisor.log`.

### 12.2 Arranque automático com o Windows

1. Faça duplo clique em `C:\mt5-bot\install_autostart.bat` (ou num `cmd`: `cd /d C:\mt5-bot` e `install_autostart.bat`).
   **Sucesso:** aparece `Task 'MT5 Bot Supervisor' registered.` com o programa, a pasta de início (`Start in: C:\mt5-bot`) e o gatilho (ao iniciar sessão, com 1 minuto de atraso).
2. Teste já:
   ```
   schtasks /Run /TN "MT5 Bot Supervisor"
   ```
   **Sucesso:** abre a janela do supervisor e depois a do bot.
3. Opções: `install_autostart.bat -DelayMinutes 3` (mais tempo para o MT5 ligar) ou `install_autostart.bat -BotArgs "--dry-run"`.
4. Para remover: `uninstall_autostart.bat`.

**Alternativa manual (Agendador de Tarefas):** `taskschd.msc` > **Create Task...**; **General**: nome `MT5 Bot Supervisor`, **Run only when user is logged on**; **Triggers**: **At log on**, o seu utilizador, **Delay task for: 1 minute**; **Actions**: **Start a program**, Program `C:\mt5-bot\start_supervisor.bat`, **Start in** `C:\mt5-bot`; **Conditions**: desmarque **Start the task only if the computer is on AC power**; **Settings**: desmarque **Stop the task if it runs longer than**, escolha **Do not start a new instance**.

A tarefa corre na sua sessão do Windows (é aí que o MT5 corre). Depois de um reinício do PC, só arranca quando a sessão iniciar. O início de sessão automático (`netplwiz`) resolve isso, mas qualquer pessoa com acesso físico entra na sua conta. Decida conscientemente.

### 12.3 O terminal MT5

* Se o MT5 não estiver aberto, o bot abre-o sozinho (usa `mt5_path` ou procura `terminal64.exe` em Program Files, preferindo pastas com "STARTRADER") e espera até 60 s que ligue à corretora. Marque **Save password** no login para o terminal entrar sozinho.
* Se o terminal ficar "congelado" (sem ticks durante 10 minutos em horário de mercado, com ligação aparentemente activa), o bot fecha esse terminal e volta a abri-lo, **só** se todas as posições do bot tiverem SL e TP. No máximo uma vez a cada 30 minutos, com espera crescente.
* Recomendado: também copie o atalho do MT5 para a pasta de arranque (Windows+R, `shell:startup`).

### 12.4 Energia e actualizações do Windows

1. Impedir suspensão e hibernação, num `cmd` como administrador:
   ```
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /hibernate off
   ```
   **Sucesso:** os comandos terminam sem mensagens.
2. **Definições > Windows Update > Opções avançadas > Horas de atividade:** cubra o seu horário de negociação.
3. Use uma **UPS** para o PC e o router e, se possível, uma internet de reserva (router 4G). O bot reconecta-se sozinho (5 s até 120 s) e, com o supervisor e o arranque automático, recupera de reinícios.

### 12.5 Ficheiro de saúde

O `health.json` (na pasta do projecto) é reescrito a cada minuto com: hora do último ciclo, hora do último tick, capital, posições abertas, campeão por símbolo, paragens activas e estado do optimizador. Pode ser lido por uma ferramenta de monitorização externa. `python mt5_bot.py --status` mostra-o formatado.

### 12.6 Notificações Telegram (opcional)

1. No Telegram, fale com **@BotFather**, envie `/newbot` e siga as instruções. Guarde o **token** (algo como `123456789:AA...`).
2. Envie qualquer mensagem ao seu novo bot. Depois abra no navegador (troque `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   e copie o número em `"chat":{"id":...}`. Esse é o **chat id**.
3. Grave os dois valores como variáveis de ambiente (nunca em ficheiros do projecto):
   ```
   setx TELEGRAM_BOT_TOKEN "123456789:AA-o-seu-token"
   setx TELEGRAM_CHAT_ID "123456789"
   ```
   **Sucesso:** `SUCCESS: Specified value was saved.` (ou `ÊXITO: ...`).
4. No `config.json`: `"telegram_enabled": true`. Feche e abra um **novo** `cmd` e reinicie o bot.

Mensagens enviadas: arranque e paragem (incluindo se a conta é REAL), promoções e despromoções com motivo, resumo diário, disjuntor de drawdown, limite diário, kill switch, terminal reiniciado, deslizamento excessivo, erros repetidos e o disjuntor de reinícios do supervisor. O token nunca é escrito no registo.

---

## 13. Operações do dia a dia

| Quero... | Como | O que acontece às posições |
|---|---|---|
| Parar com calma | **Ctrl+C** na janela do bot (ou do supervisor). | Ficam abertas, protegidas por SL/TP no servidor. |
| Parar e fechar tudo (emergência) | `cd /d C:\mt5-bot` e `type nul > KILL` | O bot fecha **as suas** posições (não toca nas outras) e termina. O supervisor não o reinicia. |
| Voltar a usar depois do kill switch | `del KILL` e arranque de novo. | Enquanto o `KILL` existir, o bot fecha as posições e sai sempre que arrancar. |
| Pausar novas entradas sem parar | `"dry_run": true` no `config.json` (recarga automática). | As posições existentes continuam com SL/TP e gestão normal. |
| Desfazer uma troca de estratégia | `python mt5_bot.py --history`, escolha o número da decisão que instalou o campeão pretendido e `python mt5_bot.py --rollback 7` | O bot em funcionamento adopta-o na vela seguinte. Fica registado como `ROLLBACK`. |
| Retomar depois do disjuntor de drawdown | Leia `state\<conta>\RESUME_REQUIRED.txt`, perceba o que aconteceu e depois `python mt5_bot.py --resume` (ou apague esse ficheiro). | O pico de capital recomeça no capital desse momento. |

Com várias contas com estado, os comandos `--history`, `--rollback` e `--resume` usam a conta registada no `health.json`. Para escolher outra: `--account STARTRADER-Demo_12345678`.

**O disjuntor de drawdown:** se o capital cair mais de `max_drawdown_from_peak_pct` (12% por omissão) face ao pico registado, o bot fecha todas as suas posições, deixa de abrir novas, notifica e cria `RESUME_REQUIRED.txt`. Nem um novo dia, nem um reinício, nem uma estratégia o desligam: é o único ponto em que uma pessoa tem de intervir, de propósito. Depósitos e levantamentos são detectados e não contam como lucro nem como drawdown.

**O limite diário:** ao perder `max_daily_loss_pct_*` desde o início do dia do servidor, fecha as posições do bot e pára até ao dia seguinte (recomeça sozinho).

---

## 14. Adicionar uma estratégia e ajustar limites

### 14.1 Uma estratégia nova é um ficheiro

1. Crie `mt5bot\strategies\a_minha.py`:

```python
"""Exemplo: rompimento de N velas confirmado pela EMA. Sem vantagem comprovada."""

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, crosses_above, crosses_below, sides_from
from .registry import register


@register
class AMinha(Strategy):
    name = "a_minha"
    description = "Rompimento de N velas na direcção da EMA longa."
    param_space = (
        Param.int_range("lookback", 20, 60, 10),
        Param.int_range("trend_ema", 100, 200, 50),
        Param.float_range("sl_mult", 1.5, 3.0, 0.5),
        Param.float_range("tp_mult", 2.0, 4.0, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return 3 * int(params["trend_ema"]) + 2

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        close = np.asarray(bars["close"], dtype=float)
        trend = indicators.ema(close, int(params["trend_ema"]))
        upper = indicators.rolling_max_prev(bars["high"], int(params["lookback"]))
        lower = indicators.rolling_min_prev(bars["low"], int(params["lookback"]))
        with np.errstate(invalid="ignore"):
            buy = crosses_above(close, upper) & (close > trend)
            sell = crosses_below(close, lower) & (close < trend)
        return sides_from(buy, sell)
```

2. Regras do contrato: `compute` recebe **só velas fechadas** e devolve, para cada vela, `+1` (compra), `-1` (venda) ou `0`; o valor da vela `i` só pode usar dados até `i` (sem olhar para o futuro); nada de rede, ficheiros ou ordens. SL e TP são `ATR(14) x sl_mult` e `ATR(14) x tp_mult`, e tudo o resto (volume, filtros, limites) é da camada de risco.
3. Corra os testes. Os testes de contrato em `tests\test_strategies.py` (determinismo, ausência de lookahead, limites de parâmetros) passam a correr automaticamente para a nova estratégia. No fim desse ficheiro há um modelo para um teste específico.
   ```
   pytest -q tests\test_strategies.py
   ```
4. A estratégia entra no próximo ciclo de optimização (se `enabled_strategies` for `null` ou a incluir).

### 14.2 Ajustar limites de parâmetros com segurança

* Mantenha poucos valores por parâmetro. Cada valor extra aumenta o número de combinações, a penalização por tentativas e o risco de sobre-optimização.
* Prefira limites com significado (por exemplo períodos que cobrem de uma sessão a uma semana) a intervalos enormes.
* `sl_mult` e `tp_mult` têm de ficar dentro dos limites rígidos de `mt5bot\limits.py` (0.5 a 6 e 0.5 a 10); os testes falham se não ficarem.
* Depois de alterar, corra `pytest -q` e `python mt5_bot.py --backtest --offline` para ver o efeito nos portões, sem promover nada.

---

## 15. Credenciais por variáveis de ambiente

**Por omissão não precisa disto:** o bot usa a conta já com sessão iniciada no terminal. As variáveis servem para o bot entrar numa conta específica.

1. Num `cmd`:
   ```
   setx MT5_LOGIN "12345678"
   setx MT5_PASSWORD "a-sua-palavra-passe"
   setx MT5_SERVER "STARTRADER-Demo"
   ```
2. Feche e abra um **novo** `cmd`. Verifique sem mostrar a palavra-passe: `echo %MT5_LOGIN% %MT5_SERVER%`.
3. Defina as **três** ou nenhuma; só uma parte faz o bot recusar arrancar.
4. Para remover:
   ```
   reg delete HKCU\Environment /v MT5_LOGIN /f
   reg delete HKCU\Environment /v MT5_PASSWORD /f
   reg delete HKCU\Environment /v MT5_SERVER /f
   ```

O `setx` guarda os valores em texto simples no registo do Windows do seu utilizador. Proteja a sua conta do Windows. Nunca ponha palavras-passe ou tokens no `config.json` nem noutros ficheiros do projecto: o bot nunca os lê de ficheiros e nunca os escreve no registo.

---

## 16. Backtest e optimização manual

Nenhum destes comandos envia ordens nem altera o campeão por si só.

* Relatório walk-forward fora da amostra de todas as estratégias, com o motivo de cada recusa (liga-se ao MT5 para actualizar o histórico):
  ```
  python mt5_bot.py --backtest
  ```
  Com `--offline` usa só o histórico já guardado. O relatório fica também em `reports\<conta>\backtest_*.txt`.
* Uma estratégia com parâmetros fixos sobre todo o histórico (resultado **dentro da amostra**, não é validação):
  ```
  python mt5_bot.py --backtest --offline --strategy donchian --params "{\"channel\": 50, \"sl_mult\": 2.0, \"tp_mult\": 3.0}"
  ```
* Um ciclo de optimização agora (os resultados ficam no registo e um bot em funcionamento adopta os sobreviventes como desafiantes em sombra):
  ```
  python mt5_bot.py --optimize-once
  ```

---

## 17. Resolução de problemas

| Sintoma / mensagem | Causa provável | Solução |
|---|---|---|
| Não abre nenhuma operação | **Modo de observação** (sem campeão ainda), **NoTrade** (nada passou os portões), `dry_run` activo, fora de `trading_hours_server`, spread alto, paragem diária ou de drawdown activa, já existe posição do bot nesse símbolo, lote mínimo arriscaria demais. | `python mt5_bot.py --status` mostra a linha de estado e as paragens. Depois `findstr /C:"OBSERVATION" /C:"NO TRADE" /C:"rejected" /C:"no trade:" bot.log`. Na maior parte dos casos é o comportamento pretendido. |
| `REAL MONEY account ... one deliberate confirmation` (código 3) | Conta real sem confirmação. | Secção 8.2. |
| `account_mode is "demo_only" but ...` (código 3) | A conta não corresponde ao `account_mode`. | Mude de conta no terminal ou ajuste `account_mode`. |
| `mt5.initialize() failed: (-10003, 'IPC initialize failed ...')` | MT5 fechado ou a arrancar; Python de 32 bits; vários terminais; permissões diferentes (um como administrador). | Abra o MT5 e espere que ligue. Confirme 64 bits. Defina `mt5_path`. Corra os dois com o mesmo nível de permissões. |
| O terminal não abre sozinho | `terminal64.exe` instalado fora de Program Files ou várias instalações. | Defina `"mt5_path": "C:\\Program Files\\NOME DA PASTA\\terminal64.exe"` (botão direito no atalho do MT5 > Propriedades > Destino). |
| `Symbol 'EURUSD' not found (also tried broker suffixes)` | O símbolo tem outro nome ou não existe nessa conta. | Market Watch > Show All; copie o nome exacto para `symbols`. Se houver vários sufixos possíveis, escreva o nome completo (por exemplo `EURUSD.m`). |
| `Symbol EURUSD resolved to broker name EURUSD.m` | Informativo: o sufixo foi detectado. | Nada a fazer. |
| Horas erradas nas entradas ou `trading_hours_server` "não funciona" | A janela usa a hora do **servidor**, não a do PC. | Veja `Broker server time offset detected: UTC+2.00 h` no `bot.log` e a hora no cabeçalho do Market Watch. Ajuste a janela à hora do servidor. Com o mercado fechado o desvio não é detectável e o bot usa UTC+2 até chegarem ticks. |
| `no history returned ... Max bars in chart` | Histórico insuficiente para a optimização. | Secção 6, passo 3; abra um gráfico do símbolo nesse período para o terminal carregar histórico. |
| `cannot compute exposure (order_calc_margin unavailable)` | O terminal não calculou a margem. | Verifique a ligação; o bot recusa entrar até conseguir medir a exposição (falha em segurança). |
| `State file ... is unreadable or corrupt` | `risk_state.json` foi editado ou danificado. | Corrija-o ou apague-o aceitando que o limite diário e o pico recomeçam do capital actual. |
| `Restart circuit breaker` no `supervisor.log` | O bot falhou 5 vezes em 30 minutos. | Veja o fim do `bot.log`, corrija a causa e arranque de novo `start_supervisor.bat`. |
| `config.json changed but is invalid; keeping the previous config` | Erro de escrita no ficheiro. | A mensagem diz a chave e o problema; corrija. O bot continua com a configuração anterior. |

Códigos de retorno (retcode) mais comuns:

| Código | Nome | Significado e o que fazer |
|---|---|---|
| 10004 | Requote | O preço mudou. O bot repete até 3 vezes com preço novo. |
| 10006 | Request rejected | A corretora recusou. Leia o comentário; se persistir, contacte o suporte. |
| 10014 | Invalid volume | Volume fora do mínimo, máximo ou passo do símbolo. Veja **Specification**. |
| 10015 | Invalid price | Preço inválido, muitas vezes mercado muito rápido ou símbolo sem cotação. |
| 10016 | Invalid stops | SL/TP perto demais do preço. O bot já respeita o `Stops level`; se acontecer, a corretora alterou-o. |
| 10018 | Market is closed | Mercado fechado. O bot espera 15 minutos antes de tentar de novo nesse símbolo. |
| 10019 | No money | Margem insuficiente. Reduza o risco ou o número de posições. |
| 10027 | AutoTrading disabled by client | Algo Trading desligado no terminal (secção 6). |
| 10030 | Unsupported filling mode | O bot escolhe IOC, FOK ou RETURN conforme o símbolo; se persistir, reporte o `filling_mode` à corretora. |

Códigos de saída do bot:

| Código | Significado |
|---|---|
| 0 | Saída normal (Ctrl+C, kill switch com posições fechadas, contagem cancelada). |
| 1 | Erro inesperado, ou kill switch sem conseguir fechar tudo (as posições ficam com SL/TP). O supervisor reinicia. |
| 2 | Falha no arranque (configuração, ligação, Algo Trading, símbolo). O supervisor reinicia com espera. |
| 3 | Paragem de segurança: conta real sem confirmação, `account_mode` errado, troca de conta ou de tipo de conta durante a execução. O supervisor **não** reinicia. |
| 4 | (supervisor) Disjuntor de reinícios. |

---

## 18. Executar os testes

Os testes substituem o pacote `MetaTrader5` por uma versão falsa, por isso **nunca** tocam no terminal nem na conta, e correm também em Linux ou macOS.

```
cd /d C:\mt5-bot
.venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
```

**Sucesso:** a última linha diz `342 passed in ...` (cerca de 3 a 5 minutos; o teste de passeio aleatório corre a optimização completa em muitas sementes).

O que é verificado, entre outros: política de contas (demo, real, confirmação, código 3, pastas separadas), tectos de risco e todos os comportamentos proibidos, disjuntor de drawdown e `--resume`, ausência de lookahead em todas as estratégias, P&L calculado à mão com spread, comissão e a regra pessimista, **NoTrade em dados de passeio aleatório em praticamente todas as sementes** (mesmo sem custos), descoberta de uma vantagem injectada, promoção com sombra, histerese e cooldown, despromoção, reconciliação e reparação de SL, supervisor e o seu disjuntor, escrita atómica, recarga do `config.json`, e uma simulação ponta a ponta (observação, optimização, promoção, operações, despromoção e nova optimização).

---

## 19. Limitações e pressupostos

**Não verificado sem um terminal MT5 real em Windows** (o código foi desenvolvido e testado em Linux com um MT5 simulado):

* a interacção real com o terminal e a corretora: abertura automática do `terminal64.exe` via `initialize(path=...)`, reinício de um terminal congelado (PowerShell `Stop-Process` filtrado pelo caminho exacto), nomes e valores dos campos devolvidos pela STARTRADER (`trade_tick_value_loss`, `swap_mode`, `margin_mode`, comissões nos negócios);
* os scripts `install_autostart.ps1`/`.bat` e `start_supervisor.bat` (escritos para Windows 10/11 e PowerShell 5.1, não executados);
* a baixa prioridade do processo de optimização no Windows (`BELOW_NORMAL_PRIORITY_CLASS`);
* o caminho de instalação por omissão do MT5 da STARTRADER: o bot procura qualquer pasta de Program Files com `terminal64.exe` e prefere as que contêm "STARTRADER". Se não encontrar, defina `mt5_path`.

**Pressupostos e escolhas deliberadas:**

* As velas do MT5 são preços BID. No backtest a compra paga o spread completo da vela (equivale a meio spread de cada lado do preço médio) e as saídas de vendas usam o ASK (BID + spread da vela).
* Walk-forward: cada janela de teste fica com as operações **abertas** nela (a saída é resolvida nas velas seguintes); no treino só contam operações abertas **e fechadas** dentro da janela de treino, para que nenhum dado de teste influencie a escolha dos parâmetros.
* A penalização por tentativas usa o Sharpe deflacionado (Bailey e López de Prado) com o número total de tentativas do ciclo, embora a escolha dos parâmetros seja feita só no treino: é deliberadamente conservador. Com `min_deflated_sharpe` = 0.90 e os restantes portões, é provável que, em dados reais de forex, o bot fique muitas vezes em `NoTrade`. Baixar para 0.80 aceita mais candidatos e mais risco de sobre-optimização.
* `min_shadow_trades` **ou** `min_shadow_days`: a regra dos dias exige ainda `min_shadow_trades_floor` (10) operações, para não promover com 2 operações em 14 dias.
* O cooldown de 7 dias conta a partir da última troca (promoção, despromoção ou rollback); a primeira promoção não espera. As despromoções nunca esperam.
* A sombra calcula os sinais sobre as últimas `bars` velas (1000); indicadores recursivos (EMA) iniciados nessa janela podem diferir minimamente dos do backtest sobre 12 meses.
* O "dia" dos limites diários é o dia do servidor da corretora. O drawdown e o limite diário usam o capital de **toda a conta** (inclui operações manuais ou de outros robôs).
* A API Python do MT5 não fornece o horário de sessões de cada símbolo nem um calendário económico: o mercado é considerado aberto quando há ticks a mudar, com fins de semana e `holidays` para a detecção de terminal congelado; o filtro de notícias usa um CSV mantido por si.
* Swaps positivos são ignorados (pessimista); modos de swap diferentes de "pontos" e "moeda do depósito" contam como zero.
* Comissões: detectadas no histórico de negócios da conta; sem histórico usa 7 por lote (pessimista). Em contas só com spread defina `"commission_per_lot": 0`.
* Contas "contest" são tratadas como demo. Python 3.10 a 3.12, 64 bits, `MetaTrader5>=5.0.5430`, `numpy>=1.26`.

**Limitações:**

* Backtests e resultados em sombra são estimativas. O deslizamento real no SL, gaps de fim de semana e notícias, alterações de spread e de execução da corretora podem fazer uma perda real exceder 1R.
* A qualidade dos dados do terminal (velas em falta, spreads registados) afecta a optimização.
* A latência de um PC em casa é maior do que a de um servidor junto da corretora; estratégias de períodos curtos sofrem mais.
* Uma posição por símbolo por bot. Não corra duas instâncias com o mesmo `magic`.
* Fechar a janela do bot à força não permite uma saída ordenada; prefira Ctrl+C ou o kill switch. As posições continuam protegidas por SL/TP.
