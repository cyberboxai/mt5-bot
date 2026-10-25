# Bot de negociação para MetaTrader 5, versão 3 (Python, Windows)

Guia completo de instalação e operação do bot `mt5_bot.py`, que negoceia automaticamente através do terminal MetaTrader 5 (MT5) instalado no seu PC Windows. Foi pensado para a corretora STARTRADER, mas funciona com qualquer corretora MT5. A versão 3 acrescenta uma equipa de agentes especializados, um conselheiro de IA opcional e o painel web CYBERBOX Trading Office.

## Índice

1. [O que é isto e o que mudou na v3](#1-o-que-é-isto-e-o-que-mudou-na-v3)
2. [Aviso de risco (leia primeiro)](#2-aviso-de-risco-leia-primeiro)
3. [Requisitos](#3-requisitos)
4. [Instalar o Python](#4-instalar-o-python)
5. [Instalar o MT5 e entrar numa conta](#5-instalar-o-mt5-e-entrar-numa-conta)
6. [Preparar o MT5 para automação](#6-preparar-o-mt5-para-automação)
7. [Instalar o projecto](#7-instalar-o-projecto)
8. [Selecção de conta: demo ou real](#8-selecção-de-conta-demo-ou-real)
9. [Como funciona o motor adaptativo](#9-como-funciona-o-motor-adaptativo)
10. [A equipa de agentes](#10-a-equipa-de-agentes)
11. [O conselheiro Claude (opcional)](#11-o-conselheiro-claude-opcional)
12. [O painel CYBERBOX Trading Office](#12-o-painel-cyberbox-trading-office)
13. [Referência completa do config.json](#13-referência-completa-do-configjson)
14. [Primeira execução e fase de observação](#14-primeira-execução-e-fase-de-observação)
15. [Execução sem supervisão](#15-execução-sem-supervisão)
16. [Operações do dia a dia](#16-operações-do-dia-a-dia)
17. [Estender o bot: estratégias, agentes e painel](#17-estender-o-bot-estratégias-agentes-e-painel)
18. [Credenciais por variáveis de ambiente](#18-credenciais-por-variáveis-de-ambiente)
19. [Backtest, optimização, simulação e relatórios](#19-backtest-optimização-simulação-e-relatórios)
20. [Resolução de problemas](#20-resolução-de-problemas)
21. [Executar os testes](#21-executar-os-testes)
22. [Limitações e pressupostos](#22-limitações-e-pressupostos)

---

## 1. O que é isto e o que mudou na v3

O bot liga-se ao terminal MT5 aberto no seu PC (pacote oficial `MetaTrader5`), lê velas fechadas, escolhe sozinho qual estratégia pode negociar e envia ordens a mercado **sempre com stop loss (SL) e take profit (TP)** no servidor da corretora.

A v3 acrescenta três peças ao núcleo da v2, sem lhe tirar nenhuma protecção. As três são opcionais: o núcleo determinístico continua completo sozinho.

| Área | v2 | v3 |
|---|---|---|
| Decisão de entrada | O campeão propõe; a camada de risco aprova ou recusa. | Uma equipa de 21 agentes especializados analisa cada proposta do campeão (secção 10). Só pode reduzir o tamanho ou vetar. Com `team_mode: auto` (omissão), o bot negoceia como a v2 até a equipa provar, fora da amostra, que acrescenta valor. |
| Inteligência artificial | Nenhuma. | Conselheiro Claude opcional e desligado por omissão (secção 11). Só pode reduzir ou vetar. Qualquer falha (sem chave, sem crédito, rede, lentidão, resposta inválida) dá exactamente a mesma decisão que sem conselheiro. |
| Visibilidade | `bot.log`, `health.json` e relatórios de texto. | Painel web **CYBERBOX Trading Office** (secção 12): um escritório animado ligado aos eventos reais do bot, 10 painéis, repetição das últimas 24 horas, dossiers de decisão e modo de demonstração. Corre num processo separado e é só de leitura por omissão. |
| Explicações | Motivo de cada promoção ou recusa no registo. | Cada decisão guarda um dossier completo: entradas, votos, pesos, conselheiro, risco, execução e resultado. |
| Aprendizagem | Optimização walk-forward e validação em sombra. | O mesmo, mais pesos limitados por agente e por regime, calibração das confianças, meta-etiquetagem validada fora da amostra e uma ablação periódica que mede o valor de cada agente. |
| Simulação | Testes com um MT5 falso. | `--simulate`: um mercado sintético que corre o bot verdadeiro sem corretora, para conhecer o painel e a equipa sem risco. |
| Relatórios | Texto diário e semanal. | Também um relatório HTML diário em português. |

**O que não mudou e nunca pode mudar por configuração:** o campeão é a única origem possível de uma ordem; todas as ordens levam SL e TP; os tectos de `mt5bot/limits.py`, o disjuntor de drawdown, o limite diário, o kill switch e a camada de risco são os mesmos e aplicam-se em todos os modos. **Nenhum agente nem o conselheiro pode** aumentar o tamanho, transformar um "não" num "sim", enviar ou alterar ordens, nem alargar ou remover um SL. Não há código gerado em tempo de execução.

### 1.1 O que a v2 trouxe face à v1

O que se manteve da v1: volume calculado pelo risco, SL e TP em todas as ordens, `order_check` antes de cada envio, repetição em caso de requote, kill switch, reconexão automática, registo rotativo `bot.log`, limite de perda diária persistente e testes com um MT5 simulado.

| Área | v1 | v2 |
|---|---|---|
| Contas | Recusava contas reais (`allow_live`). | Aceita demo **ou** real. Dinheiro real exige uma confirmação explícita, feita uma única vez (secção 8). |
| Estratégia | Uma estratégia fixa (cruzamento de EMA). | Biblioteca de 7 estratégias com parâmetros limitados, optimização walk-forward, validação em sombra (shadow), promoção e despromoção automáticas, e `NoTrade` quando nada demonstra vantagem. |
| Segurança | Limites no `config.json`. | Camada de risco separada com tectos rígidos no código (`mt5bot/limits.py`) que nenhuma configuração ou estratégia ultrapassa, disjuntor de drawdown e comportamentos proibidos testados. |
| Autonomia | Arranque manual. | Supervisor com reinício automático, arranque com o Windows, abertura e reinício do terminal MT5, reconciliação com a corretora, recarga do `config.json`, ficheiro de estado `health.json`, relatórios diários e semanais e notificações Telegram opcionais. |
| Estado | Um ficheiro `bot_state.json`. | Uma pasta por conta em `state\<servidor>_<login>\` com bases de dados SQLite (diário de operações, registo de estratégias, decisões). |

### 1.2 Estrutura do projecto

```
mt5_bot.py              ponto de entrada (python mt5_bot.py ...)
supervisor.py           supervisor que mantém o bot (e o painel) a correr
config.json             configuração
mt5bot\                 código do bot: risk.py, limits.py, engine.py, optimizer.py, bot.py, ...
mt5bot\strategies\      biblioteca de estratégias (um ficheiro por estratégia)
mt5bot\team\            equipa de agentes: protocolo, agregação, aprendizagem, ablação
mt5bot\team\agents\     os 21 agentes, por departamento
mt5bot\advisor\         conselheiro Claude opcional (estados, orçamento, esquemas, ocultação de segredos)
mt5bot\sim\             mercado sintético e terminal simulado (--simulate)
panel\                  painel web (servidor só com a biblioteca padrão do Python)
panel\static\           página, estilos e scripts do painel (tudo local, sem CDN)
tests\                  testes automáticos (pytest)
start_bot.bat           arranca só o bot
start_supervisor.bat    arranca o supervisor (recomendado para uso contínuo; arranca também o painel)
start_panel.bat         arranca só o painel (ou a demonstração, com --demo)
install_autostart.bat   regista o arranque automático no Agendador de Tarefas
uninstall_autostart.bat remove esse registo
```

Dentro de cada pasta de conta (`state\<servidor>_<login>\`) ficam `registry.sqlite` (diário de operações, estratégias, capital) e, na v3, `office.sqlite` (eventos, dossiers, agentes, pesos, chamadas ao conselheiro e auditoria das acções de controlo).

---

## 2. Aviso de risco (leia primeiro)

> * **Nenhum software garante lucro.** Este bot também não. Pode perder dinheiro, incluindo todo o capital da conta.
> * **A maioria dos investidores particulares que negoceiam CFD e forex perde dinheiro.** As próprias corretoras publicam percentagens de contas perdedoras, normalmente entre 70% e 80%.
> * **Sistemas adaptativos podem sobre-optimizar (overfitting):** encontram padrões no passado que são apenas acaso e que não se repetem. A v2 foi desenhada para combater isso (validação fora da amostra, penalização por número de tentativas, validação em sombra), mas nenhuma técnica elimina este risco.
> * **O objectivo do bot é maximizar a expectativa ajustada ao risco e aos custos, fora da amostra, e preservar o capital acima de tudo.** Quando nenhuma estratégia demonstra vantagem, a decisão correcta é **não negociar**. O bot pode ficar dias, semanas ou meses sem abrir operações. **Isso é intencional, não é uma avaria.**
> * Resultados de backtest são estimativas. A execução real tem deslizamento (slippage), spreads variáveis, saltos de preço (gaps) e falhas de internet, energia ou da corretora.
> * **Mais complexidade não é mais vantagem.** A equipa de agentes e o conselheiro de IA não criam vantagem: só podem reduzir o tamanho ou vetar uma proposta que já passou os portões, e também podem recusar operações que teriam dado lucro. A equipa é medida continuamente por ablação e, com `team_mode: auto`, só passa a contar depois de bater o núcleo base fora da amostra; se deixar de o bater, é substituída pelo núcleo base (a v2).
> * **O conselheiro Claude custa dinheiro**, cobrado pela Anthropic a si. Os limites do bot (chamadas por hora, custo por dia) são estimativas locais a partir dos tokens; defina sempre também um limite de gasto na consola da Anthropic.
> * **A simulação e o modo de demonstração usam um mercado sintético.** Lucros, pesos ou vitórias da equipa que lá apareçam não são evidência sobre mercados reais.
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
4. **View > Market Watch** (Ctrl+M), botão direito, **Show All**. Anote o nome exacto do símbolo, por exemplo `EURUSD` ou `EURUSD.m`. O bot detecta sufixos sozinho (secção 20), mas confirmar não custa.
5. O cabeçalho do Market Watch mostra a **hora do servidor**. O bot detecta automaticamente a diferença para UTC a partir dos ticks; `trading_hours_server` usa a hora do servidor.

---

## 7. Instalar o projecto

1. Crie a pasta e entre nela:
   ```
   mkdir C:\mt5-bot
   cd /d C:\mt5-bot
   ```
2. Copie para `C:\mt5-bot` todos os ficheiros e pastas do projecto (`mt5_bot.py`, `supervisor.py`, `config.json`, as pastas `mt5bot`, `panel` e `tests`, os ficheiros `.bat` e `.ps1`, `requirements.txt`, `requirements-dev.txt`, `README.md`).
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
5. (Opcional) Só para o conselheiro Claude (secção 11), instale também os pacotes opcionais. O bot e o painel funcionam sem eles:
   ```
   pip install anthropic keyring
   ```
   **Sucesso:** `Successfully installed anthropic-... keyring-...`. O `keyring` só serve para guardar a chave no Gestor de Credenciais do Windows.
6. Com o MT5 aberto e com sessão iniciada, teste a ligação:
   ```
   python -c "import MetaTrader5 as m; print(m.initialize(), m.account_info())"
   ```
   **Sucesso:** imprime `True AccountInfo(login=..., trade_mode=0, ...)`. `trade_mode=0` é demo, `2` é real.

---

## 8. Selecção de conta: demo ou real

O bot negoceia na conta que estiver com sessão iniciada no terminal (ou na conta indicada pelas variáveis de ambiente da secção 18). Ao arrancar mostra uma faixa com o número da conta (mascarado no registo, por exemplo `***321`), o servidor, `DEMO` ou `REAL MONEY`, o capital, a moeda, a alavancagem e o tipo de conta (hedging ou netting).

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

## 10. A equipa de agentes

Em linguagem simples: sempre que o campeão quer abrir uma operação, a proposta passa por uma reunião. Especialistas verificam os dados, o regime de mercado, a sessão, as notícias e a exposição; os analistas de estratégia votam a favor ou contra; a Directora de Investimentos agrega os votos com pesos aprendidos; a Gestora de Risco tem a última palavra. O resultado é um **multiplicador entre 0 e 1** que só pode **reduzir** o volume que a camada de risco calculou. A equipa não cria operações: sem proposta do campeão não há reunião.

Cada agente é uma classe Python no código (`mt5bot\team\agents\`), corre com tempo limite e isolado dos outros. Se um agente falhar, a reunião continua sem ele (e os agentes de segurança falham no sentido seguro: sem a Escriturária de Dados, por exemplo, não há entradas).

### 10.1 Os 21 agentes

| Nome | Função | Departamento | Pode decidir | Não pode | id |
|---|---|---|---|---|---|
| Dina | Escriturária de Dados | Inteligência de Mercado | pausar entradas quando os dados estão maus | abrir ou fechar posições | `data_clerk` |
| Rui | Detector de Regime | Inteligência de Mercado | classificar o regime de mercado | votar ou dimensionar trades | `regime_detector` |
| Sara | Analista de Sessões e Liquidez | Inteligência de Mercado | reduzir o tamanho em horas pouco líquidas | aumentar o tamanho | `session_analyst` |
| Nuno | Sentinela de Notícias e Calendário | Inteligência de Mercado | bloquear entradas perto de eventos de alto impacto | abrir trades ou ler instruções de notícias | `news_sentinel` |
| Carla | Analista de Correlação e Exposição | Inteligência de Mercado | reduzir ou bloquear risco duplicado entre moedas | aumentar a exposição | `correlation_analyst` |
| Tomás | Analista de Tendência | Mesa de Estratégia | votar a favor ou contra (e propor, se tiver o campeão) | dimensionar acima da base | `trend_analyst` |
| Mara | Analista de Reversão à Média | Mesa de Estratégia | votar a favor ou contra (e propor, se tiver o campeão) | dimensionar acima da base | `mean_reversion_analyst` |
| Bruno | Analista de Rupturas e Volatilidade | Mesa de Estratégia | votar a favor ou contra (e propor, se tiver o campeão) | dimensionar acima da base | `breakout_analyst` |
| Mateus | Analista Multi-Período | Mesa de Estratégia | votar com o enviesamento do período superior | propor trades | `mtf_analyst` |
| Lia | Meta-Etiquetadora | Mesa de Estratégia | votar com a probabilidade validada fora da amostra (ou abster-se) | propor trades | `meta_labeler` |
| Rita | Gestora de Risco | Risco e Controlo | vetar qualquer entrada (veto final) | ser vencida por votos, pesos ou pelo conselheiro | `risk_manager` |
| Gil | Guardião | Risco e Controlo | congelar entradas ou accionar o kill switch | abrir trades ou relaxar limites | `guardian` |
| Edu | Operador de Execução | Execução | enviar a ordem aprovada com SL/TP e verificar spread e preço | alterar o tamanho aprovado | `execution_trader` |
| Paula | Gestora de Posições | Execução | apertar stops, fechos parciais e de tempo (só reduzem risco) | alargar ou remover um SL | `position_manager` |
| Quim | Investigador Quantitativo | Investigação e Revisão | correr a optimização walk-forward e o modo sombra | promover sem portões | `quant_researcher` |
| Vera | Auditora de Validação | Investigação e Revisão | sinalizar sobreajuste e fugas de dados | promover estratégias | `validation_auditor` |
| Pedro | Analista de Desempenho | Investigação e Revisão | actualizar pesos e calibração, pôr agentes no banco | mexer no risco | `performance_analyst` |
| Rosa | Repórter | Investigação e Revisão | escrever relatórios e notificações | decidir trades | `reporter` |
| Olavo | Engenheiro de Operações | Operações e Liderança | vigiar o terminal, a máquina, o painel e o conselheiro | decidir trades | `ops_engineer` |
| Ciro | Estratega Claude (ligação ao conselheiro) | Operações e Liderança | pedir ao conselheiro para reduzir ou vetar | aumentar o tamanho, criar trades ou mexer em ordens | `claude_strategist` |
| Isabel | Directora de Investimentos (Orquestradora) | Operações e Liderança | conduzir o protocolo e submeter o pedido de trade | ultrapassar o Risco ou o Guardião | `cio` |

* **Nunca vão para o banco:** `data_clerk`, `risk_manager`, `guardian` e `ops_engineer`.
* **Não podem ser desligados na configuração:** `data_clerk`, `risk_manager`, `guardian`, `execution_trader`, `ops_engineer` e `cio`.
* Os restantes desligam-se em `team.agents`, por exemplo `"team": {"agents": {"news_sentinel": false}}`.
* Os quatro analistas de estratégia são **famílias**: o analista dono do campeão (Tomás para `ema_cross`, Bruno para `donchian` e `session_breakout`, Mara para `rsi_reversion`) apresenta a proposta; os outros votam com a sua leitura independente do mercado.

### 10.2 O protocolo de decisão

```
 Campeão propõe BUY ou SELL num símbolo      <- a ÚNICA origem possível de uma ordem
        |
        v
 1. Contexto, em paralelo, com tempo limite por agente (250 ms):
    Dina (dados) | Rui (regime) | Sara (sessão) | Nuno (notícias) | Carla (correlação)
    pré-verificação da Rita (risco) | Gil (Guardião)
        |   dados maus, notícia próxima, congelamento ou veto prévio?  -> SEM TRADE
        v
 2. Votos da Mesa de Estratégia: Tomás, Mara, Bruno, Mateus
    2b. Lia (meta-etiquetadora): probabilidade validada fora da amostra, ou abstém-se
        |
        v
 3-4. Agregação pela Isabel: confiança calibrada x peso aprendido de cada votante
      confiança agregada < 0,52, desacordo > 0,5 ou nenhum votante a favor -> SEM TRADE
      multiplicador m: 0 em 0,52, sobe até 1 em 0,65, vezes (1 - desacordo)
      e vezes as reduções pedidas pela Sara e pela Carla
        |
        v
 5. Camada de risco (Rita = mt5bot/risk.py, o mesmo código da v2): veto final
        |
        v
 6. Conselheiro Claude (opcional; só se ainda for para entrar; no máximo 1500 ms):
    confirmar (x1) | reduzir (x0 a x1) | vetar (x0) | indisponível = sem efeito
        |
        v
 7. Execução pelo Edu: volume base x min(m, conselheiro), sempre com SL e TP,
    com nova verificação de risco no momento do envio
        |
        v
 Dossier guardado em office.sqlite; o resultado é medido quando a operação fecha
```

**Regra da Directora por regime (em `full`):** se a estratégia campeã já tiver, no regime actual, pelo menos `regime_min_trades` (20) resultados e o limite superior a 95% da sua expectativa for negativo, a Directora não a consulta nesse regime (sem trade), mesmo que a equipa concorde.

O diagrama descreve o modo `full`. Em `baseline` (e em `auto` antes de a equipa provar valor) só os bloqueios de segurança da secção 10.5 e a camada de risco podem impedir uma entrada; as reduções e vetos da equipa, da Directora e do conselheiro ficam registados como contrafactuais.

O multiplicador final nunca passa de 1 e é aplicado **depois** de a camada de risco calcular o volume. Se o volume reduzido ficar abaixo do lote mínimo sem cumprir o risco, a entrada é recusada (nunca é arredondada para cima).

### 10.3 Os poderes da Gestora de Risco e do Guardião

* **Rita (Gestora de Risco)** é a camada de risco da v2 (`RiskManager.approve_entry`). O veto dela é final: nenhum voto, peso, conselheiro ou modo o ultrapassa, e ela nunca vai para o banco. Faz uma pré-verificação barata no início (para não gastar uma chamada ao conselheiro numa entrada já bloqueada) e a verificação definitiva no momento do envio.
* **Gil (Guardião)** é um caminho de código **independente**: o bot chama-o directamente no seu ciclo, fora da reunião, por isso continua a vigiar mesmo que a equipa falhe. Verifica invariantes a cada ciclo:
  * posições do bot sem SL/TP depois da reparação automática;
  * mais posições do que os tectos rígidos;
  * posições na corretora que não estão no diário;
  * risco aberto acima do limite (com 10% de tolerância) ou risco por operação acima do tecto;
  * ordens enviadas enquanto a negociação estava parada.

  Qualquer violação **congela novas entradas** em todos os modos (as posições abertas continuam protegidas). O congelamento levanta-se sozinho depois de 3 verificações limpas seguidas (`team.guardian_freeze_clear_checks`). Três situações graves pedem o **kill switch** (cria o ficheiro `KILL`, fecha as posições do bot e pára): uma posição sem SL/TP durante 3 verificações, posições acima dos tectos, ou ordens enviadas com a negociação parada. Pode desligar este último passo com `"guardian_kill_enabled": false` (fica só o congelamento).

### 10.4 Aprendizagem: pesos, calibração, banco e ablação

Tudo é aprendido devagar, com limites, e só com operações **já fechadas** (nunca com o futuro):

* **Resultado de cada proposta:** quando uma proposta fica resolvida, o bot sabe se a direcção estava certa, mesmo que a equipa a tenha recusado, porque a estratégia campeã negoceia também em sombra (o mesmo sinal, na vela seguinte, com as regras do backtest).
* **Pesos (algoritmo Hedge):** cada votante tem um peso por regime de mercado (tendência, lateral, volatilidade alta, ilíquido) e um peso global. Depois de cada resultado, os agentes cuja confiança acertou mais do que a média sobem um pouco e os outros descem (taxa `learning_rate` 0,05, decaimento `weight_decay` 0,01 para 1,0). Os pesos ficam sempre entre `weight_min` 0,25 e `weight_max` 2,0. Enquanto um regime não tiver `warmup_outcomes` (30) resultados, a agregação usa pesos uniformes nesse regime.
* **Calibração:** com pelo menos 50 resultados, a confiança declarada por cada agente é corrigida (escalonamento de Platt) para corresponder à frequência real de acerto. O painel mostra a fiabilidade de cada agente.
* **Meta-etiquetadora (Lia):** uma regressão logística simples (numpy) treinada só com propostas resolvidas e validada numa parte final que não viu (walk-forward). Se não superar a base nessa parte, abstém-se.
* **Banco por desempenho:** a cada ablação (de 24 em 24 horas), se um votante tiver pelo menos `bench_min_samples` (100) decisões e o seu valor acrescentado for zero ou negativo, vai para o **banco**: continua a votar em sombra, mas o voto deixa de contar. Regressa quando a evidência voltar a favor (valor acrescentado acima de +0,02).
* **Banco por erros:** um agente que falhe `agent_max_failures` (3) vezes em 10 minutos é posto de lado e reiniciado com espera crescente. Os agentes de segurança nunca são postos de lado.
* **Ablação:** o bot volta a correr as decisões resolvidas com e sem cada agente (e com e sem o conselheiro) e compara o Sharpe por proposta, com um intervalo de confiança bootstrap emparelhado. O resultado fica no painel (separador **Equipa**) e em `python mt5_bot.py --ablation-report`.

Para recomeçar a aprendizagem do zero (por exemplo depois de mudar muito a configuração): `python mt5_bot.py --reset-team-weights`. O bot em funcionamento mantém o que tem em memória até reiniciar.

### 10.5 Os modos da equipa: `team_mode`

| Modo | O que faz | Quando usar |
|---|---|---|
| `auto` (omissão) | Usa o núcleo base (v2) **até** a ablação mostrar, com pelo menos `ablation_min_samples` (50) decisões resolvidas e 90% de confiança, que a equipa completa supera o núcleo base fora da amostra. Depois passa a `full`, e volta a `baseline` se deixar de superar. | Sempre, salvo razão concreta. |
| `full` | As reduções e vetos da equipa e do conselheiro aplicam-se já. | Para observar a equipa em demo antes de haver evidência. |
| `baseline` | Negoceia exactamente como a v2 (multiplicador 1). A equipa e o conselheiro continuam a opinar e ficam registados como contrafactuais, para medir o seu valor. | Para comparar, ou se quiser a v2 com o painel. |

**Em todos os modos** aplicam-se os bloqueios de segurança: dados maus, alerta de fuga de dados da Auditora, congelamento do Guardião, pausa pelo painel e camada de risco. Em `baseline` (e em `auto` antes de haver prova) **o conselheiro não altera o volume**: o veredicto fica registado e é medido, mas só conta em `full`. É uma escolha deliberada: o núcleo base continua a ser exactamente a v2 validada.

Pode forçar o modo numa execução com `--team-mode full`, `--team-mode baseline` ou `--team-mode auto` (sobrepõe-se ao `config.json`). A equipa não se desliga por completo: para ter o comportamento da v2, use `baseline`.

### 10.6 O dossier de decisão

Cada reunião produz um dossier em `office.sqlite`, visível no painel (separador **Registo de decisões**, ou clique numa reunião no escritório):

* proposta (símbolo, lado, estratégia, parâmetros), regime e sessão;
* a opinião de cada agente: posição (comprador, vendedor, neutro, veto, abstém-se), confiança bruta e calibrada, peso, motivo e se estava no banco;
* a agregação: confiança agregada, desacordo, número de concordantes, vetos e o contrafactual da equipa completa;
* o conselheiro: estado, veredicto, multiplicador, motivo e latência;
* o risco: a pré-verificação e a verificação no envio;
* a execução: enviada ou não, volume, preço, SL, TP e deslizamento;
* o resultado: o R da proposta (da sombra) e o R efectivo da operação real.

---

## 11. O conselheiro Claude (opcional)

O conselheiro é um segundo par de olhos baseado no modelo Claude, da Anthropic. **Está desligado por omissão** e o bot é completo sem ele. Quando está ligado:

| Tarefa | Modelo por omissão | O que pode fazer |
|---|---|---|
| Revisão antes de cada entrada (só quando a equipa já quer entrar) | `claude-haiku-5-5` (rápido) | Confirmar, reduzir o tamanho (x0 a x1) ou vetar. |
| Explicação de um dossier, em português | `claude-haiku-5-5` | Texto de leitura, marcado como comentário de IA. |
| Revisão depois de uma operação fechar | `claude-haiku-5-5` | Notas e hipóteses (ver abaixo). |
| Briefing diário | `claude-opus-5-5` (mais forte) | Inclinar os pesos dos votantes **só para baixo**, no máximo 20% e por no máximo 24 horas. |
| Investigação semanal | `claude-opus-5-5` | Sugerir parâmetros **da grelha já existente** para o próximo ciclo de optimização; passam pelos mesmos portões, sem atalhos. |
| Sugestões de gestão de posições (desligado: `manage_positions`) | `claude-haiku-5-5` | Só apertar: manter, apertar o stop, fecho parcial ou fecho. Nunca alargar. |

**O que nunca pode fazer:** aumentar o tamanho, transformar um "sem trade" numa entrada, criar operações, enviar ou alterar ordens, alargar ou remover um SL, mexer na Gestora de Risco ou no Guardião, ou propor código. As respostas têm de cumprir um esquema JSON estrito; qualquer coisa fora do esquema é ignorada. Lembre-se também (secção 10.5) de que em `baseline`, e em `auto` antes de a equipa provar valor, o conselheiro é consultado (e cobrado) para se medir o seu valor, mas o veredicto não altera o volume. Se não quiser esse custo antes de haver prova, deixe `enabled` em `false` ou ponha `review_trades` em `false`.

**O que é enviado à Anthropic:** só uma lista fechada de valores de mercado e da proposta (símbolo, lado, estratégia, regime, ATR, spread, votos agregados, percentagens de risco). **Nunca** o número de conta, o servidor, saldos em dinheiro, nomes de ficheiros, a chave ou tokens. Texto externo (por exemplo títulos de notícias) vai marcado como dados não confiáveis e não pode dar instruções.

### 11.1 Ligar o conselheiro

1. Crie uma conta em https://console.anthropic.com, carregue crédito e crie uma chave de API (começa por `sk-ant-`).
2. **Defina já um limite de gasto mensal na consola da Anthropic** (página de limites da organização). Os limites do bot são estimativas locais; o limite da consola é o único que a Anthropic garante.
3. Instale o pacote opcional, com o ambiente virtual activo:
   ```
   cd /d C:\mt5-bot
   .venv\Scripts\activate
   pip install anthropic
   ```
   **Sucesso:** `Successfully installed anthropic-...`.
4. Guarde a chave **fora do projecto**, de uma destas formas:
   * Como variável de ambiente do utilizador (a forma mais simples):
     ```
     setx ANTHROPIC_API_KEY "sk-ant-a-sua-chave"
     ```
     **Sucesso:** `SUCCESS: Specified value was saved.` Feche e abra um **novo** `cmd` (os processos já abertos não vêem a variável). Confirme sem mostrar a chave: `if defined ANTHROPIC_API_KEY (echo definida) else (echo em falta)`.
   * Ou no Gestor de Credenciais do Windows (precisa de `pip install keyring`):
     ```
     python mt5_bot.py --set-claude-key
     ```
     Cole a chave (não aparece no ecrã) e prima Enter. **Sucesso:** `Key stored in the operating system credential store (service 'mt5bot').`
5. No `config.json`, acrescente (ou altere) o bloco:
   ```json
   "claude": {"enabled": true, "max_calls_per_hour": 20, "max_daily_cost_usd": 0.50}
   ```
6. Reinicie o bot. **Sucesso:** no `bot.log` aparece `Claude advisor DISABLED -> ONLINE: key present` e, no painel, o distintivo **Claude: disponível**.

A chave **nunca** é lida do `config.json`, nunca é escrita no registo, na base de dados nem nas respostas do painel, e é ocultada em todas as mensagens de erro (incluindo tracebacks). O bot volta a procurar a chave de 5 em 5 minutos (`key_recheck_seconds`). Com `--set-claude-key` não precisa de reiniciar; uma variável definida com `setx` só é vista por processos arrancados depois (feche os `cmd` e reinicie o bot ou o supervisor).

Para remover a chave: `reg delete HKCU\Environment /v ANTHROPIC_API_KEY /f` (variável) ou, no Gestor de Credenciais do Windows, apague a entrada `mt5bot`.

### 11.2 Orçamento, custo e latência

| Chave | Omissão | Porquê |
|---|---|---|
| `max_calls_per_hour` | `20` | Mais do que as entradas possíveis num período M15 com 3 símbolos, menos do que o necessário para uma avaria custar caro. Tecto 600. |
| `max_daily_cost_usd` | `0.50` | Estimativa local a partir dos tokens e da tabela `claude.prices`. Ao atingir o limite, o conselheiro fica em `BUDGET_CAPPED` até à janela seguinte e o bot continua sem ele. Tecto 50. |
| `latency_budget_ms` | `1500` | O máximo que uma entrada espera pela revisão. Depois disso a revisão conta como indisponível (sem efeito) e a entrada segue com a decisão da equipa. Tecto 5000. |
| `timeout_seconds` | `10` | Limite de cada pedido HTTP (as tarefas de fundo podem demorar mais do que a revisão de entrada). Tecto 60. |
| `cache_ttl_seconds` | `300` | A mesma situação dentro de 5 minutos reutiliza a resposta, sem nova chamada. |

O orçamento conta também as chamadas falhadas e é reposto a partir do histórico depois de um reinício (um reinício não dá orçamento novo). Os custos da API são da sua responsabilidade; confirme os preços actuais da Anthropic e actualize `claude.prices` se mudarem.

### 11.3 Estados do conselheiro

| Estado (painel) | Significado | O que o bot faz |
|---|---|---|
| `DISABLED` (desligado) | `claude.enabled` é `false`, `--no-claude`, ou o pacote `anthropic` não está instalado. | Nunca importa o pacote nem faz chamadas. |
| `NO_KEY` (sem chave) | Não há `ANTHROPIC_API_KEY` nem chave no Gestor de Credenciais. | Volta a procurar de 5 em 5 minutos. |
| `ONLINE` (disponível) | A funcionar. | Usa-o dentro do orçamento. |
| `DEGRADED` (degradado) | Falhas temporárias: limite de pedidos (429), sobrecarga (529), erro do servidor, tempo esgotado, rede. | Espera crescente com variação aleatória (30 s a 15 min) e tenta de novo. |
| `OFFLINE_NO_CREDIT` (sem crédito) | Crédito esgotado ou problema de pagamento. | Pára e só testa raramente: 30 minutos, depois o dobro, até 6 horas. Carregue crédito. |
| `OFFLINE_AUTH` (chave inválida) | Chave inválida ou sem permissão (401/403). | Pára. Sai deste estado quando aparecer uma chave **diferente**. |
| `BUDGET_CAPPED` (limite de orçamento) | O limite por hora ou por dia foi atingido. | Retoma sozinho quando a janela reiniciar. |

As mudanças de estado ficam no painel (separador **Conselheiro Claude**), no `bot.log` e, se activar o Telegram, numa notificação.

### 11.4 A garantia de funcionamento sem o conselheiro

Qualquer falha do conselheiro dá **exactamente a mesma decisão** que um bot sem conselheiro. Isto é verificado automaticamente, sem rede e sem chave, por um cliente falso que simula: chave inválida (401), sem permissão (403), crédito esgotado (400 e 402), limite de pedidos (429), sobrecarga (529), erro do servidor (500), tempo esgotado, falha de ligação, JSON inválido, violação do esquema (tamanho acima de 1, chaves a mais), resposta cortada, recusa, resposta extremamente lenta, sem chave, desligado, orçamento esgotado e pacote em falta. Para cada caso, em vários cenários de votação (unânime, dividido, contraditório e com os agentes reais), o teste compara a decisão com a de um bot sem conselheiro; outro teste confirma o mesmo nos modos `full` e `baseline`:

```
pytest -q tests\test_authority.py tests\test_advisor.py
```

**Sucesso:** todos passam. Os mesmos testes verificam que um conselheiro aleatório (fuzzing) nunca aumenta o tamanho nem cria operações, que texto de notícias com instruções maliciosas não muda nada, e que a chave nunca aparece em registos, na base de dados nem em tracebacks.

**Para desligar numa execução**, sem mexer na configuração:
```
python mt5_bot.py --no-claude
```

### 11.5 Desvios deliberados e notas técnicas

* **Temperatura:** a especificação previa respostas com temperatura baixa. Os modelos actuais (`claude-haiku-5-5`, `claude-opus-5-5`) não aceitam o parâmetro `temperature`, por isso o bot não o envia. A estabilidade vem do esquema JSON estrito, de veredictos limitados e da cache por situação.
* As chamadas usam saídas estruturadas (`output_config` com esquema JSON), o nível de esforço configurado (`effort_fast` `low`, `effort_deep` `medium`) e cache do prompt de sistema. As chamadas profundas pedem o mecanismo de recurso do servidor em caso de recusa e, se esse mecanismo não for aceite, repetem sem ele.
* O cliente é criado sem repetições automáticas (`max_retries=0`): o próprio bot gere as esperas, para nunca martelar a API.
* Cada chamada fica registada em `office.sqlite` (finalidade, modelo, resultado, tokens, custo estimado, latência, hash e versão ocultada do prompt, resposta). A chave nunca é guardada.

---

## 12. O painel CYBERBOX Trading Office

O painel é uma página web que mostra, ao vivo, o que o bot está a fazer: um escritório animado onde cada personagem é um agente real, e dez painéis com números. Corre num **processo separado** do bot, abre as bases de dados **só para leitura** e, por omissão, **não tem nenhum botão que mexa na negociação**. Se o painel falhar, travar ou for fechado, o bot não é afectado (há um teste automático que mata o painel a meio e confirma que o bot continua).

Tudo é servido pelo próprio PC (sem CDN nem serviços externos) e, por omissão, só aceita ligações do próprio PC (`127.0.0.1`).

### 12.1 Abrir o painel

**Com o supervisor (recomendado):** `start_supervisor.bat` arranca o bot **e** o painel (com `"panel": {"enabled": true}`, a omissão). Se o painel cair, o supervisor reinicia-o; se cair 10 vezes numa hora, desiste dele e o bot continua.

**Sozinho**, por exemplo para ver um bot que já está a correr:
```
cd /d C:\mt5-bot
start_panel.bat
```
**Sucesso:** a janela mostra `CYBERBOX Trading Office at http://127.0.0.1:8765/ (local only, controls off (...))`. Abra **http://127.0.0.1:8765/** no navegador.

Opções úteis: `start_panel.bat --port 8800` (outra porta), `start_panel.bat --state-dir state\STARTRADER-Demo_12345678` (escolher a conta). O registo do painel fica em `panel.log`, ao lado do `bot.log`.

### 12.2 Modo de demonstração (sem MT5, sem chave, sem dinheiro)

Para conhecer o painel antes de ligar o bot a uma conta:
```
cd /d C:\mt5-bot
start_panel.bat --demo
```
O painel arranca uma simulação do bot verdadeiro sobre um mercado sintético (EURUSD, GBPUSD, USDJPY), com a equipa em `full` e um conselheiro simulado que passa de propósito por falhas (degradado, sem crédito) para ver como o bot reage. Abra **http://127.0.0.1:8765/**. Uma faixa roxa diz **SIMULAÇÃO** em todas as páginas.

* Os dados ficam em `state_demo\` (pode apagar a pasta quando quiser).
* A janela mostra um token de uso único para experimentar as acções de controlo: `DEMO: control actions use this one-time token: ...`.
* `--demo-speed 4` (velas por segundo) e `--demo-fast-forward 300` (velas iniciais à velocidade máxima) controlam o ritmo.
* **Nada do que aparece na simulação é evidência sobre mercados reais.**

A mesma simulação, sem painel, corre com `python mt5_bot.py --simulate` (secção 19).

### 12.3 O escritório

O separador **Escritório** mostra dez salas: Sala de Negociação (a mesa de estratégia), Laboratório de Investigação, Gabinete de Risco, Cabine do Conselheiro Claude, Redacção (sessões e notícias), Sala do Conselho (reuniões), Mesa de Execução, Sala de Operações, Canto da Análise e Sala de Descanso (o banco).

* **Cada personagem é um agente** e o que faz vem dos eventos reais do bot: escrever é analisar, ler é rever, falar é dar uma opinião (o balão mostra o que disse), ir à Sala do Conselho é entrar numa reunião, levar um papel à Mesa de Execução é uma ordem aprovada, um sinal vermelho é um veto, ir para a Sala de Descanso é estar no banco. Uma personagem com um aviso vermelho teve um erro. O Ciro aparece "ausente" quando o conselheiro não está disponível.
* **Os ecrãs nas paredes** mostram o preço e o regime de cada símbolo, a perda diária e o risco aberto, a próxima notícia, os spreads, as métricas da máquina e o P&L do dia.
* **A reunião de decisão** aparece num cartão: votos de cada analista, veredicto do conselheiro, risco e resultado. "Abrir dossier" mostra a decisão completa.
* **Clique numa personagem** (ou use a lista "Agentes", acessível por teclado) para ver a ficha: o que pode e não pode decidir, tarefa actual, peso por regime e a sua evolução, calibração, acerto por regime, contribuição medida pela ablação, opiniões e mensagens recentes.
* **Fluxo de eventos:** à direita, com filtros por agente, símbolo e gravidade. Os eventos de decisão abrem o dossier.
* **Repetição (últimas 24 h):** arraste a barra ou carregue em **Reproduzir** (10x a 900x) para rever o dia. **Ao vivo** volta ao presente.

O escritório respeita a opção do sistema "reduzir movimento" e pára de desenhar quando o separador do navegador não está visível.

### 12.4 Os dez painéis

| Separador | O que mostra |
|---|---|
| **Visão geral** | Capital, P&L de hoje, 7 e 30 dias e total, posições abertas, modo da equipa, estado do conselheiro, curva de capital e saldo, indicadores de risco (perda diária usada, risco aberto, distância ao disjuntor), campeões e posições abertas. |
| **Desempenho** | Métricas das operações reais e em sombra, distribuição dos R, retornos mensais, R acumulado, expectativa móvel, equipa contra núcleo base (repetição fora da amostra) e comprar e manter o instrumento, como referência. |
| **Trades** | O diário de operações (reais ou sombra), com filtros, ordenação, exportação CSV, MAE e MFE em R, deslizamento, comissão estimada, regime, sessão e multiplicador da equipa. Clique numa linha para ver o dossier. |
| **Equipa** | Classificação dos agentes (votos, acerto, Brier, peso, valor acrescentado, disponibilidade), pesos ao longo do tempo, agentes no banco e os departamentos. |
| **Estratégias e Investigação** | Campeões e desafiantes, ciclos walk-forward, candidatos e o portão que rejeitou cada um, promoções e despromoções, progresso em sombra e hipóteses vindas do conselheiro. |
| **Risco e Exposição** | VaR histórico (estimativa), pior cenário do risco aberto, exposição por símbolo e por moeda, correlação dos retornos, cada limite com o valor configurado, o tecto e o uso actual, e os disjuntores. |
| **Mercados** | Sessões, regime e probabilidades por símbolo, ER e ADX, percentil de volatilidade, spread por hora e próximas notícias de alto impacto. |
| **Conselheiro Claude** | Estado, chamadas e custo estimado de hoje, latência, valor acrescentado medido, vereditos, erros por classe, histórico de estados, último briefing, investigação e chamadas recentes. |
| **Saúde do sistema** | Bot a correr ou não, último ciclo e tick, reinícios, fila de eventos, métricas de 24 horas (CPU, memória, ciclo, latência do MT5, idade do tick, tamanho das bases e do registo, disco), acções de controlo, alertas, auditoria e o fim do `bot.log` (com segredos ocultados). |
| **Registo de decisões** | Todas as reuniões, com pesquisa e filtros. Clique para abrir o dossier. |

Todos os gráficos têm o botão **Tabela** (os mesmos números em texto) e **CSV**. Com poucos dados, um gráfico diz "Dados insuficientes" em vez de mostrar números enganadores. O botão **Tema** alterna entre escuro e claro e **EN** muda para inglês.

**Como são calculadas as métricas:**

* **R:** resultado de uma operação dividido pelo risco inicial (a distância ao SL). +2R é ganhar o dobro do que se arriscou.
* **Expectativa:** R médio por operação. **Factor de lucro:** soma dos ganhos a dividir pela soma das perdas. **Taxa de acerto:** percentagem de operações com R positivo. Precisam de pelo menos 10 operações.
* **Sharpe, Sortino e Calmar:** a partir das variações **diárias** do capital, anualizadas com 252 dias; precisam de pelo menos 10 dias. Sortino usa só as variações negativas; Calmar é o retorno anualizado a dividir pelo drawdown máximo.
* **VaR histórico a 1 dia (95%):** a perda diária que só foi excedida em 5% dos dias registados. É uma **estimativa** do passado, não um limite; precisa de 20 dias.
* **MAE e MFE:** o pior e o melhor ponto que a operação atingiu antes de fechar, em R.
* **Valor acrescentado:** diferença de Sharpe por proposta com e sem o agente (ou o conselheiro), medida na ablação.

### 12.5 Acções de controlo (desligadas por omissão)

O painel pode ter três botões no separador **Saúde do sistema**: **Pausar novas entradas**, **Retomar entradas** e **Kill switch**. Só funcionam se **as duas** condições se verificarem:

1. `"panel": {"control_actions_enabled": true}` no `config.json`;
2. um token definido no ambiente (nunca em ficheiros):
   ```
   setx MT5BOT_PANEL_TOKEN "uma-frase-longa-e-aleatoria-com-mais-de-20-caracteres"
   ```
   Feche e abra um **novo** `cmd` e reinicie o supervisor (ou o painel).

Cada acção pede o token (escrito na altura, nunca guardado no navegador) e uma confirmação, é verificada contra pedidos de outros sites (CSRF e origem), está limitada a 5 por minuto e fica registada na auditoria (separador **Saúde do sistema** e `office.sqlite`), com sucesso ou recusa. O que fazem:

| Acção | Efeito | As posições abertas |
|---|---|---|
| Pausar novas entradas | Cria `PAUSE_ENTRIES` na pasta da conta. O bot deixa de abrir operações. | Continuam com SL/TP e gestão normal. |
| Retomar entradas | Apaga `PAUSE_ENTRIES`. Não desliga disjuntores, limites nem o Guardião. | Sem alteração. |
| Kill switch | Cria o ficheiro `KILL`. | O bot fecha **as suas** posições e pára (secção 16). |

O painel nunca pode relaxar limites, aumentar o risco, enviar ordens ou mudar a configuração.

### 12.6 Acesso a partir de outro computador

**Recomendado: não exponha a porta.** Use um túnel SSH ou uma VPN, e o painel continua a ouvir só em `127.0.0.1`.

* **Túnel SSH** (com o servidor OpenSSH do Windows activo no PC do bot, em **Definições > Sistema > Funcionalidades opcionais > Servidor OpenSSH**): no outro computador,
  ```
  ssh -N -L 8765:127.0.0.1:8765 o_seu_utilizador@ip-do-pc-do-bot
  ```
  e abra **http://127.0.0.1:8765/** nesse computador.
* **VPN** (por exemplo WireGuard ou Tailscale): ligue os dois computadores à VPN e use o túnel SSH através dela, ou o modo remoto abaixo apenas dentro da VPN.

**Modo remoto (só se souber o que está a fazer):** `"panel": {"host": "0.0.0.0"}` faz o painel aceitar ligações da rede. Nesse caso o painel **recusa arrancar** sem `MT5BOT_PANEL_TOKEN` definido, e **todas** as páginas passam a pedir o token numa página de entrada (que cria uma sessão com cookie protegido; as tentativas são limitadas). Nunca abra a porta no router para a internet.

O painel nunca mostra número de conta completo, servidor, palavras-passe, chaves ou tokens, nem os caminhos das pastas.

---

## 13. Referência completa do config.json

Regras do formato JSON: texto entre aspas duplas, `true`/`false`/`null` em minúsculas, sem vírgula depois do último item, e em caminhos do Windows cada `\` escreve-se `\\`. O bot valida tudo ao arrancar e em cada recarga; chaves desconhecidas são recusadas com uma mensagem clara. As chaves da v1 que foram removidas (`allow_live`, `risk_per_trade_pct`, `max_daily_loss_pct`, `fast_ema`, `slow_ema`, `atr_period`, `sl_atr_mult`, `tp_atr_mult`, `state_file`) dão um erro que diz o que as substitui.

O `config.json` que acompanha o projecto lista as chaves mais usadas; as restantes usam o valor por omissão desta tabela. "Tecto" é o máximo rígido do código: o bot recusa valores acima dele.

### 13.1 Mercado e conta

| Chave | Significado | Omissão | Intervalo seguro | Tecto |
|---|---|---|---|---|
| `symbols` | Símbolos a negociar (o bot detecta sufixos como `.m`). | `["EURUSD"]` | 1 a 5 símbolos líquidos | |
| `timeframe` | Período das velas: `M1`, `M5`, `M15`, `M30`, `H1`, `H4`. | `"M15"` | `M15` a `H1` | |
| `bars` | Velas fechadas enviadas às estratégias em tempo real. | `1000` | 1000 a 2000 | 20000 |
| `history_months` | Meses de histórico para a optimização. | `12` | 12 a 24 | 60 |
| `magic` | Número que identifica as ordens deste bot. Só mexe em posições com este número. | `510001` | Único por instância | |
| `account_mode` | `any`, `demo_only` ou `real_only` (secção 8). | `"any"` | | |
| `real_money_confirmed` | Confirmação permanente para contas reais. | `false` | | |

### 13.2 Risco (camada de risco)

| Chave | Significado | Omissão | Intervalo seguro | Tecto |
|---|---|---|---|---|
| `risk_per_trade_pct_demo` | % do capital arriscada até ao SL (demo), comissão incluída. | `0.5` | 0.25 a 1 | 2.0 |
| `risk_per_trade_pct_real` | Idem, contas reais. | `0.25` | 0.1 a 0.5 | 2.0 |
| `max_daily_loss_pct_demo` | Perda diária máxima (dia do servidor). Ao atingir: fecha posições do bot e pára até ao dia seguinte. | `2.0` | 1 a 3 | 5.0 |
| `max_daily_loss_pct_real` | Idem, contas reais. | `1.5` | 1 a 2 | 5.0 |
| `max_open_positions_total` | Máximo de posições do bot em simultâneo. | `3` | 1 a 3 | 5 |
| `max_total_open_risk_pct` | Soma do risco até ao SL de todas as posições abertas. | `2.0` | 1 a 2 | 4.0 |
| `max_symbol_exposure_x` | Exposição nocional por símbolo, em múltiplos do capital. | `5.0` | 2 a 5 | 10.0 |
| `max_drawdown_from_peak_pct` | Disjuntor: queda do capital face ao pico que fecha tudo e exige intervenção humana (secção 16). | `12.0` | 8 a 15 | 20.0 |

### 13.3 Execução, filtros e custos

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

### 13.4 Motor adaptativo

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

### 13.5 Operação

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
| `telegram_enabled` | Notificações Telegram (secção 15.6). | `false` |
| `kill_switch_file` | Ficheiro de paragem de emergência. | `"KILL"` |
| `state_root` / `reports_dir` / `health_file` / `log_file` | Pastas e ficheiros de estado, relatórios, saúde e registo. | `"state"` / `"reports"` / `"health.json"` / `"bot.log"` |
| `mt5_path` | Caminho do `terminal64.exe`. `null` = usar o terminal aberto ou procurar em Program Files (prefere pastas com "STARTRADER"). | `null` |
| `mt5_init_timeout_ms` | Tempo de espera do arranque do terminal. | `60000` |

**Recarga automática:** o bot relê o `config.json` a cada minuto. Se o novo ficheiro for inválido, mantém a configuração anterior e escreve o motivo no `bot.log`. As chaves de segurança só podem ser **apertadas** em funcionamento (por exemplo baixar o risco); para as alargar, ou para mudar `symbols`, `timeframe`, `magic`, `account_mode`, `real_money_confirmed`, pastas ou `mt5_path`, reinicie o bot. Os tectos de `mt5bot/limits.py` só mudam alterando o código.

### 13.6 Conselheiro Claude: bloco `claude`

Todos os valores ficam dentro de um bloco, por exemplo `"claude": {"enabled": true, "max_daily_cost_usd": 0.25}`. As chaves que não indicar usam a omissão. A chave de API **nunca** vai aqui (secção 11.1).

| Chave | Significado | Omissão | Intervalo | Tecto |
|---|---|---|---|---|
| `enabled` | Liga o conselheiro. | `false` | | |
| `model_fast` / `model_deep` | Modelos para revisões e explicações / para briefing e investigação. | `"claude-haiku-5-5"` / `"claude-opus-5-5"` | identificadores de modelo | |
| `effort_fast` / `effort_deep` | Nível de esforço (`low`, `medium`, `high`, `xhigh`, `max`). | `"low"` / `"medium"` | | |
| `max_tokens_fast` / `max_tokens_deep` | Tamanho máximo da resposta. | `1024` / `4096` | 64 a 16000 / 256 a 16000 | 16000 |
| `timeout_seconds` | Limite de cada pedido. | `10` | 1 a 60 | 60 |
| `latency_budget_ms` | Espera máxima de uma entrada pela revisão. | `1500` | 50 a 5000 | 5000 |
| `max_calls_per_hour` | Chamadas por hora (todas as finalidades). | `20` | 1 a 600 | 600 |
| `max_daily_cost_usd` | Custo estimado máximo por dia (UTC). `0` = nenhuma chamada. | `0.50` | 0 a 50 | 50 |
| `cache_ttl_seconds` | Reutilização da resposta para a mesma situação. | `300` | 0 a 86400 | |
| `prices` | Preço por milhão de tokens por modelo (`input`, `output`, `cache_read`, `cache_write`), só para a estimativa. Modelos desconhecidos usam o preço mais caro. | Haiku 0,10/0,50; Opus 4,00/20,00 | 0 a 1000 | |
| `probe_degraded_base_seconds` / `probe_degraded_max_seconds` | Espera entre tentativas em `DEGRADED`. | `30` / `900` | 5 a 3600 / até 86400 | |
| `probe_offline_seconds` / `probe_offline_max_seconds` | Espera entre testes em `OFFLINE_*` (duplica até ao máximo). | `1800` / `21600` | mínimo 300 | |
| `key_recheck_seconds` | De quanto em quanto tempo procura a chave. | `300` | 30 a 86400 | |
| `review_trades` | Revisão antes de cada entrada. | `true` | | |
| `manage_positions` | Sugestões de gestão (só apertar). | `false` | | |
| `daily_briefing` / `post_trade_reviews` / `weekly_research` / `explain_decisions` | Tarefas de fundo. | `true` | | |
| `refusal_fallback_deep` | Recurso do servidor quando o modelo profundo recusa. | `true` | | |
| `use_keyring` | Procurar a chave no Gestor de Credenciais do Windows. | `true` | | |

### 13.7 Equipa: bloco `team`

| Chave | Significado | Omissão | Intervalo | Tecto |
|---|---|---|---|---|
| `team_mode` | `auto`, `full` ou `baseline` (secção 10.5). | `"auto"` | | |
| `agents` | Desligar agentes, por exemplo `{"news_sentinel": false}`. Os de segurança não se desligam. | `{}` | | |
| `weight_min` / `weight_max` | Limites dos pesos aprendidos. | `0.25` / `2.0` | 0,05 a 1 / 1 a 5 | 0,05 / 5 |
| `learning_rate` | Velocidade de aprendizagem dos pesos. | `0.05` | 0 a 0,5 | 0,5 |
| `weight_decay` | Regresso gradual dos pesos a 1,0. | `0.01` | 0 a 0,5 | |
| `warmup_outcomes` | Resultados por regime antes de usar pesos. | `30` | 1 ou mais | |
| `min_agreement` | Votantes a favor necessários. | `1` | 1 a 21 | |
| `min_aggregate_confidence` | Abaixo disto: sem trade. | `0.52` | 0,5 a 0,95 | |
| `full_size_confidence` | A partir disto, a confiança não reduz o tamanho. Tem de ser maior do que o anterior. | `0.65` | 0,5 a 1 | |
| `max_disagreement` | Acima disto: sem trade. | `0.5` | 0 a 1 | |
| `flat_vote_weight` | Peso de um voto neutro. | `0.5` | 0 a 1 | |
| `agent_timeout_ms` | Tempo limite de cada agente numa reunião. | `250` | 10 a 5000 | 5000 |
| `agent_max_failures` / `agent_failure_window_seconds` / `agent_restart_backoff_seconds` | Falhas antes de pôr um agente de lado, janela e espera inicial (duplica). | `3` / `600` / `60` | | |
| `calibration_min_samples` | Resultados antes de calibrar a confiança. | `50` | 10 ou mais | |
| `bench_min_samples` | Decisões antes de poder ir para o banco por desempenho. | `100` | 20 ou mais | |
| `regime_min_trades` | Resultados de uma estratégia num regime antes de a Directora a poder excluir desse regime (secção 10.2). | `20` | 5 ou mais | |
| `ablation_every_hours` / `ablation_min_samples` / `ablation_confidence` | Frequência, amostra mínima e confiança da ablação. | `24` / `50` / `0.90` | 1 a 720 / 20+ / 0,5 a 0,999 | |
| `meta_min_train` / `meta_retrain_every` / `meta_holdout_fraction` / `meta_l2` | Meta-etiquetadora: amostra mínima, retreino, parte de validação e regularização. | `100` / `25` / `0.3` / `1.0` | | |
| `correlation_threshold` / `correlation_lookback_bars` | Correlação considerada risco duplicado e velas usadas. | `0.8` / `500` | 0,3 a 0,99 / 50 a 20000 | |
| `guardian_kill_enabled` | O Guardião pode pedir o kill switch (senão só congela). | `true` | | |
| `guardian_freeze_clear_checks` | Verificações limpas para levantar o congelamento. | `3` | 1 a 100 | |
| `events_retention_days` / `max_event_rows` / `dossier_retention_days` | Retenção em `office.sqlite`. | `14` / `200000` / `365` | | |

**`team.position_manager`** (gestão de posições, tudo desligado por omissão). Estas regras só reduzem risco, mas mudam as saídas com que a estratégia foi validada no backtest e na sombra; active-as conscientemente.

| Chave | Significado | Omissão | Intervalo |
|---|---|---|---|
| `breakeven_after_r` | Passa o SL para a entrada depois de +N R. | `null` | 0,25 a 10 |
| `trailing_atr_mult` | SL móvel a N x ATR (só aperta). | `null` | 0,5 a 6 |
| `time_stop_bars` | Fecha depois de N velas. | `null` | 1 a 10000 |
| `partial_close_at_r` / `partial_close_fraction` | Fecho parcial a +N R e a fracção fechada. | `null` / `0.5` | 0,25 a 10 / 0,1 a 0,9 |
| `pre_news_close_minutes` | Fecha N minutos antes de uma notícia de alto impacto. | `null` | 1 a 240 |

### 13.8 Notícias: bloco `news`

| Chave | Significado | Omissão | Intervalo |
|---|---|---|---|
| `sentinel_enabled` | Liga o Sentinela de Notícias (sem calendário, só informa "sem calendário"). | `true` | |
| `calendar_file` | CSV local `time_utc,currency,impact,title`. `null` usa `news_file` se `news_filter_enabled` estiver activo. | `null` | |
| `impact_levels` | Impactos que bloqueiam. | `["high"]` | `high`, `medium`, `low` |
| `blackout_minutes_before` / `blackout_minutes_after` | Janela sem entradas à volta de cada evento. | `30` / `15` | 0 a 1440 |
| `headlines_file` | Ficheiro local de títulos (dados não confiáveis). | `null` | |
| `advisor_headlines` | Enviar esses títulos ao conselheiro. | `false` | |

### 13.9 Painel: bloco `panel`

| Chave | Significado | Omissão | Intervalo |
|---|---|---|---|
| `enabled` | O supervisor arranca o painel. | `true` | |
| `host` | Endereço onde ouve. Diferente de `127.0.0.1` exige token (secção 12.6). | `"127.0.0.1"` | |
| `port` | Porta. | `8765` | 1024 a 65535 |
| `token_env` | **Nome** da variável de ambiente com o token (nunca o token). | `"MT5BOT_PANEL_TOKEN"` | |
| `language` / `theme` | Língua e tema iniciais. | `"pt-PT"` / `"dark"` | `pt-PT`, `en` / `dark`, `light` |
| `control_actions_enabled` | Botões de pausa, retoma e kill switch. | `false` | |
| `control_rate_per_minute` | Acções de controlo por minuto. | `5` | 1 a 60 |
| `max_streams` / `stream_poll_ms` | Ligações ao vivo simultâneas e intervalo de leitura de eventos. | `8` / `500` | 1 a 64 / 100 a 10000 |

### 13.10 Notificações v3: bloco `notifications`

As mensagens só são enviadas com o Telegram activo (secção 15.6).

| Chave | Mensagem | Omissão |
|---|---|---|
| `advisor_state` | O conselheiro ficou offline (sem crédito ou chave inválida). | `true` |
| `guardian` | O Guardião congelou as entradas. (O kill switch notifica sempre.) | `true` |
| `team_mode_changes` | O modo efectivo da equipa mudou. | `true` |
| `daily_html_report` | Gera o relatório HTML diário em `reports\<conta>\` (e avisa, se o Telegram estiver activo). | `true` |

Alterações aos blocos `claude` e `team` são aplicadas na recarga automática; o bloco `panel` só muda reiniciando o painel (ou o supervisor).

---

## 14. Primeira execução e fase de observação

### 14.1 Arrancar em demo, primeiro em dry-run

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

### 14.2 O que esperar nos primeiros dias

| Quando | O que acontece | Linha de estado |
|---|---|---|
| Minuto 0 | O bot guarda 12 meses de velas e lança a primeira optimização em segundo plano (1 a 3 minutos por símbolo num PC normal). | `OBSERVATION: collecting data; first optimisation running` |
| Minutos depois | Se nenhuma estratégia passou os portões, fica em `NoTrade` até ao próximo ciclo semanal. | `NO TRADE: no candidate passed the gates in run N; standing aside by design` |
| Dias 1 a 14+ | Se houver sobreviventes, negoceiam em sombra. | `OBSERVATION: best challenger donchian(...): shadow 12/30 trades, 5.2/14 days, mean +0.210R` |
| Primeira promoção | Com 30 operações em sombra (ou 14 dias com pelo menos 10) coerentes com o backtest. Normalmente **2 a 6 semanas**, e **pode nunca acontecer**. | `champion donchian(...) trading` |

É normal o bot não abrir nenhuma operação durante semanas. Com `--bootstrap-days 21` (ou `"bootstrap_days": 21`) obriga a pelo menos 21 dias de observação antes da primeira promoção.

### 14.3 Caminho recomendado

1. Uma semana em **demo com `--dry-run`**: confirme que não há erros e veja as decisões em `--status` e `--history`.
2. **Várias semanas em demo sem dry-run** (de preferência até ver uma promoção e algumas operações): compare o comportamento real com o que o `--history` justificou.
3. Só depois, **conta real com capital pequeno**, com `real_money_confirmed` (secção 8.2). O estado da conta real começa do zero (pastas separadas): volta a passar pela observação.

### 14.4 Ler o estado

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

## 15. Execução sem supervisão

### 15.1 O supervisor

O supervisor arranca o bot como processo filho e reinicia-o se ele terminar por erro, com espera crescente (15 s, 30 s, 60 s... até 5 minutos). Se houver 5 falhas em 30 minutos, pára de propósito (disjuntor de reinícios), notifica e deixa as posições protegidas pelo SL/TP no servidor. Não reinicia depois de saídas deliberadas: código 0 (Ctrl+C, kill switch) e código 3 (regra de segurança, conta não autorizada).

```
cd /d C:\mt5-bot
start_supervisor.bat
```

Com `"panel": {"enabled": true}` (a omissão) o supervisor arranca também o painel (secção 12) e reinicia-o se cair; se o painel cair 10 vezes numa hora, o supervisor desiste dele e o bot continua. `start_supervisor.bat --no-panel` arranca só o bot.

Para contas reais com arranque automático, use `"real_money_confirmed": true` no `config.json` (ou `start_supervisor.bat --confirm-real-money`). O registo do supervisor fica em `supervisor.log`.

### 15.2 Arranque automático com o Windows

1. Faça duplo clique em `C:\mt5-bot\install_autostart.bat` (ou num `cmd`: `cd /d C:\mt5-bot` e `install_autostart.bat`).
   **Sucesso:** aparece `Task 'MT5 Bot Supervisor' registered.` com o programa, a pasta de início (`Start in: C:\mt5-bot`) e o gatilho (ao iniciar sessão, com 1 minuto de atraso).
2. Teste já:
   ```
   schtasks /Run /TN "MT5 Bot Supervisor"
   ```
   **Sucesso:** abre a janela do supervisor e depois a do bot.
3. Opções: `install_autostart.bat -DelayMinutes 3` (mais tempo para o MT5 ligar), `install_autostart.bat -BotArgs "--dry-run"` ou `install_autostart.bat -NoPanel` (sem o painel).
4. Para remover: `uninstall_autostart.bat`.

**Alternativa manual (Agendador de Tarefas):** `taskschd.msc` > **Create Task...**; **General**: nome `MT5 Bot Supervisor`, **Run only when user is logged on**; **Triggers**: **At log on**, o seu utilizador, **Delay task for: 1 minute**; **Actions**: **Start a program**, Program `C:\mt5-bot\start_supervisor.bat`, **Start in** `C:\mt5-bot`; **Conditions**: desmarque **Start the task only if the computer is on AC power**; **Settings**: desmarque **Stop the task if it runs longer than**, escolha **Do not start a new instance**.

A tarefa corre na sua sessão do Windows (é aí que o MT5 corre). Depois de um reinício do PC, só arranca quando a sessão iniciar. O início de sessão automático (`netplwiz`) resolve isso, mas qualquer pessoa com acesso físico entra na sua conta. Decida conscientemente.

### 15.3 O terminal MT5

* Se o MT5 não estiver aberto, o bot abre-o sozinho (usa `mt5_path` ou procura `terminal64.exe` em Program Files, preferindo pastas com "STARTRADER") e espera até 60 s que ligue à corretora. Marque **Save password** no login para o terminal entrar sozinho.
* Se o terminal ficar "congelado" (sem ticks durante 10 minutos em horário de mercado, com ligação aparentemente activa), o bot fecha esse terminal e volta a abri-lo, **só** se todas as posições do bot tiverem SL e TP. No máximo uma vez a cada 30 minutos, com espera crescente.
* Recomendado: também copie o atalho do MT5 para a pasta de arranque (Windows+R, `shell:startup`).

### 15.4 Energia e actualizações do Windows

1. Impedir suspensão e hibernação, num `cmd` como administrador:
   ```
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /hibernate off
   ```
   **Sucesso:** os comandos terminam sem mensagens.
2. **Definições > Windows Update > Opções avançadas > Horas de atividade:** cubra o seu horário de negociação.
3. Use uma **UPS** para o PC e o router e, se possível, uma internet de reserva (router 4G). O bot reconecta-se sozinho (5 s até 120 s) e, com o supervisor e o arranque automático, recupera de reinícios.

### 15.5 Ficheiro de saúde

O `health.json` (na pasta do projecto) é reescrito a cada minuto com: hora do último ciclo, hora do último tick, capital, posições abertas, campeão por símbolo, paragens activas, estado do optimizador e, na v3, o modo da equipa, o congelamento do Guardião e o estado do conselheiro. Pode ser lido por uma ferramenta de monitorização externa. `python mt5_bot.py --status` mostra-o formatado.

### 15.6 Notificações Telegram (opcional)

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


### 15.7 Cópias de segurança das bases de dados

O que vale a pena guardar está na pasta da conta, `state\<servidor>_<login>\`: `registry.sqlite` (diário, estratégias, capital), `office.sqlite` (dossiers, pesos, conselheiro, auditoria) e `risk_state.json`. Com o bot a correr, **não copie os `.sqlite` com o Explorador** (pode apanhar uma escrita a meio). Use a cópia online do SQLite, que é segura com o bot ligado:

1. Crie a pasta de destino: `mkdir C:\mt5-backups`
2. Com o ambiente virtual activo, para cada base:
   ```
   cd /d C:\mt5-bot
   python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" state\STARTRADER-Demo_12345678\registry.sqlite C:\mt5-backups\registry.sqlite
   python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" state\STARTRADER-Demo_12345678\office.sqlite C:\mt5-backups\office.sqlite
   ```
   **Sucesso:** os comandos terminam sem mensagens e os ficheiros aparecem em `C:\mt5-backups`.
3. Copie também `state\STARTRADER-Demo_12345678\risk_state.json` e o seu `config.json`.

Para restaurar: pare o bot (Ctrl+C), copie os ficheiros de volta para a pasta da conta e arranque de novo. O `office.sqlite` guarda eventos durante 14 dias e dossiers durante 365 dias (`team.events_retention_days`, `team.dossier_retention_days`); apagá-lo faz a equipa recomeçar a aprendizagem, mas não mexe nas operações nem nos campeões.

### 15.8 Ficheiros de registo

| Ficheiro | Rotação | Conteúdo |
|---|---|---|
| `bot.log` | 2 MB x 5 | O bot (com segredos ocultados). |
| `supervisor.log` | 1 MB x 3 | Arranques, reinícios e o painel. |
| `panel.log` | 1 MB x 3 | O painel (ao lado do `bot.log`). |

Os ficheiros antigos (`bot.log.1`, ...) são apagados sozinhos. Para ver o fim em tempo real: `powershell -Command "Get-Content bot.log -Wait -Tail 30"`.

### 15.9 Actualizar da v2 para a v3

1. Pare o bot e o supervisor (Ctrl+C) e faça uma cópia de segurança (secção 15.7).
2. Copie os ficheiros novos do projecto por cima dos antigos, **excepto** o seu `config.json` e a pasta `state`. São novos: as pastas `panel`, `mt5bot\team`, `mt5bot\advisor` e `mt5bot\sim`, e `start_panel.bat`.
3. Actualize as dependências (a v3 não acrescenta nenhuma obrigatória):
   ```
   cd /d C:\mt5-bot
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. Corra os testes (secção 21). **Sucesso:** `... passed`.
5. Arranque como antes. O `config.json` da v2 continua válido: os blocos novos usam as omissões (equipa em `auto`, que negoceia como a v2 até haver prova; conselheiro desligado; painel ligado só em `127.0.0.1`). O bot cria sozinho o `office.sqlite` e as tabelas novas.
6. Se não quiser o painel com o supervisor: `"panel": {"enabled": false}` no `config.json`, ou volte a registar o arranque automático com `install_autostart.bat -NoPanel`.

---

## 16. Operações do dia a dia

| Quero... | Como | O que acontece às posições |
|---|---|---|
| Parar com calma | **Ctrl+C** na janela do bot (ou do supervisor). | Ficam abertas, protegidas por SL/TP no servidor. |
| Parar e fechar tudo (emergência) | `cd /d C:\mt5-bot` e `type nul > KILL` | O bot fecha **as suas** posições (não toca nas outras) e termina. O supervisor não o reinicia. |
| Voltar a usar depois do kill switch | `del KILL` e arranque de novo. | Enquanto o `KILL` existir, o bot fecha as posições e sai sempre que arrancar. |
| Pausar novas entradas sem parar | `"dry_run": true` no `config.json` (recarga automática). | As posições existentes continuam com SL/TP e gestão normal. |
| Pausar novas entradas pelo painel | **Saúde do sistema > Pausar novas entradas** (com as acções de controlo activas, secção 12.5), ou `type nul > state\<servidor>_<login>\PAUSE_ENTRIES`. Para retomar: **Retomar entradas** ou `del state\<servidor>_<login>\PAUSE_ENTRIES`. | Continuam com SL/TP e gestão normal. |
| Ver o que o bot está a fazer e porquê | Painel em http://127.0.0.1:8765/ (secção 12), ou `python mt5_bot.py --status`. | Nenhum efeito. |
| Desfazer uma troca de estratégia | `python mt5_bot.py --history`, escolha o número da decisão que instalou o campeão pretendido e `python mt5_bot.py --rollback 7` | O bot em funcionamento adopta-o na vela seguinte. Fica registado como `ROLLBACK`. |
| Retomar depois do disjuntor de drawdown | Leia `state\<conta>\RESUME_REQUIRED.txt`, perceba o que aconteceu e depois `python mt5_bot.py --resume` (ou apague esse ficheiro). | O pico de capital recomeça no capital desse momento. |

Com várias contas com estado, os comandos `--history`, `--rollback` e `--resume` usam a conta registada no `health.json`. Para escolher outra: `--account STARTRADER-Demo_12345678`.

**O disjuntor de drawdown:** se o capital cair mais de `max_drawdown_from_peak_pct` (12% por omissão) face ao pico registado, o bot fecha todas as suas posições, deixa de abrir novas, notifica e cria `RESUME_REQUIRED.txt`. Nem um novo dia, nem um reinício, nem uma estratégia o desligam: é o único ponto em que uma pessoa tem de intervir, de propósito. Depósitos e levantamentos são detectados e não contam como lucro nem como drawdown.

**O limite diário:** ao perder `max_daily_loss_pct_*` desde o início do dia do servidor, fecha as posições do bot e pára até ao dia seguinte (recomeça sozinho).

---

## 17. Estender o bot: estratégias, agentes e painel

### 17.1 Uma estratégia nova é um ficheiro

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

### 17.2 Ajustar limites de parâmetros com segurança

* Mantenha poucos valores por parâmetro. Cada valor extra aumenta o número de combinações, a penalização por tentativas e o risco de sobre-optimização.
* Prefira limites com significado (por exemplo períodos que cobrem de uma sessão a uma semana) a intervalos enormes.
* `sl_mult` e `tp_mult` têm de ficar dentro dos limites rígidos de `mt5bot\limits.py` (0.5 a 6 e 0.5 a 10); os testes falham se não ficarem.
* Depois de alterar, corra `pytest -q` e `python mt5_bot.py --backtest --offline` para ver o efeito nos portões, sem promover nada.


### 17.3 Acrescentar um agente

Um agente é uma classe Python revista e testada, como uma estratégia. O caminho mais simples (e o único que mexe nas decisões) é um **agente de contexto** que pode reduzir o tamanho ou bloquear; um agente nunca pode aumentar nada.

1. Registe-o no elenco, em `mt5bot\team\roster.py`, acrescentando um `AgentInfo` à lista `ROSTER` (id, nome, função em português e inglês, departamento, sala, ícone, o que pode e o que não pode decidir):
   ```python
   AgentInfo("holiday_watch", "Tiago", "Vigia de Feriados", "Holiday Watch", "market_intelligence", "newsroom",
             "calendar", "reduzir o tamanho em dias de feriado bancário", "aumentar o tamanho"),
   ```
   Para que participe na reunião como contexto, acrescente o id a `GATES` em `roster.py` e a `CONTEXT_STAGE` em `mt5bot\team\agents\cio.py`.
2. Crie a classe, por exemplo em `mt5bot\team\agents\market.py`:
   ```python
   class HolidayWatch(Agent):
       id = "holiday_watch"

       def analyze(self, ctx: DecisionContext) -> Opinion:
           day = datetime.fromtimestamp(ctx.market.utc_now, tz=timezone.utc).strftime("%Y-%m-%d")
           if day in self.cfg.holidays:
               self.set_state(AgentState.ANALYZING, task="feriado: metade do tamanho",
                              message=f"{ctx.market.symbol}: feriado, reduzo para metade", symbol=ctx.market.symbol)
               return self.opinion(ctx, "abstain", 0.0, [("HOLIDAY", day)], size_hint=0.5)
           return self.opinion(ctx, "abstain", 0.0, [("NO_HOLIDAY", day)])
   ```
   Regras: `size_hint` entre 0 e 1 (só reduz); `stance="veto"` bloqueia a entrada; nada de rede, ficheiros de terceiros, ordens ou `eval`; o tempo limite é `team.agent_timeout_ms`. Se a função falhar, o supervisor dos agentes isola-a e a reunião continua sem ela.
3. Acrescente a classe a `AGENT_CLASSES` em `mt5bot\team\agents\__init__.py`, na mesma posição que tem no `ROSTER`.
4. Escreva testes (siga `tests\test_team_protocol.py`): o caso normal, o caso que reduz, e que uma falha do agente não muda a decisão para pior. Actualize os testes que contam 21 agentes (`tests\test_team_integration.py`, `tests\test_panel.py`).
   ```
   pytest -q tests\test_team_framework.py tests\test_team_protocol.py tests\test_authority.py
   ```
5. O painel mostra-o sozinho (personagem, ficha, classificação). As frases que diz (`message`) aparecem nos balões e no fluxo: escreva-as em português.

### 17.4 Acrescentar um elemento ao painel

1. **Dados:** acrescente o valor ao método do separador em `panel\data.py` (por exemplo `PanelData.risk`). As bases abrem-se só para leitura; limite o tamanho do resultado e devolva `null` ou uma lista vazia quando não houver dados (nunca invente números).
2. **Teste:** em `tests\test_panel.py`, verifique o novo campo e que nada de proibido aparece (o teste `test_no_secret_or_identifier_in_any_response` cobre todos os endpoints).
3. **Visual:** no separador correspondente em `panel\static\js\views\dashboards.js`, use `chartCard` (gráfico com Tabela e CSV), `tile` (número) ou `dataTable`:
   ```js
   grid.append(chartCard({ title: "Exposição em lotes", rows, csvName: "lotes.csv", n: rows.length, min: 1,
     render: (node) => barChart(node, { bars: rows.map((r) => ({ label: r.símbolo, value: r.lotes })) }) }));
   ```
   Regras: textos com `textContent` (nunca `innerHTML`), sem scripts nem estilos inline (a política de segurança do painel bloqueia-os), cores das variáveis CSS (`var(--series-1)` ...), textos em português no `i18n.js`.
4. Verifique no navegador com `start_panel.bat --demo`.

---

## 18. Credenciais por variáveis de ambiente

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


### 18.1 Outras variáveis da v3

| Variável | Para quê | Como definir |
|---|---|---|
| `ANTHROPIC_API_KEY` | Chave do conselheiro Claude (secção 11.1). Alternativa: `python mt5_bot.py --set-claude-key` (Gestor de Credenciais do Windows). | `setx ANTHROPIC_API_KEY "sk-ant-..."` |
| `MT5BOT_PANEL_TOKEN` | Token das acções de controlo e do acesso remoto ao painel (secção 12.5). Use uma frase longa e aleatória. O nome pode mudar em `panel.token_env`. | `setx MT5BOT_PANEL_TOKEN "..."` |

Depois de `setx`, feche e abra um **novo** `cmd` e reinicie o supervisor. Nenhum destes valores é escrito em ficheiros, registos, bases de dados ou respostas do painel.

---

## 19. Backtest, optimização, simulação e relatórios

Nenhum destes comandos envia ordens nem altera o campeão de uma conta.

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

* Simulação do bot verdadeiro num mercado sintético, sem MT5, sem corretora e sem chave (o painel com `--demo` faz o mesmo e mostra-a):
  ```
  python mt5_bot.py --simulate --sim-bars 600 --sim-speed 0
  ```
  **Sucesso:** aparece `SIMULATION started in ...` e, ao fim das 600 velas, o comando termina sozinho; os dados ficam em `state_sim\` (pode apagar a pasta). Para ver uma simulação no painel, use `start_panel.bat --demo`. Opções: `--sim-seed 7` (mercado), `--sim-speed 2` (velas por segundo; 0 = o mais rápido possível), `--sim-fast-forward 300`, `--sim-advisor simulated|none|claude` (`claude` usa a API verdadeira e gasta dinheiro), `--team-mode full|baseline|auto`. Os campeões da simulação são colocados de propósito e marcados como SIMULAÇÃO; nada aprendido aqui vale para mercados reais.
* Relatório de ablação (valor acrescentado de cada agente e do conselheiro, equipa contra núcleo base):
  ```
  python mt5_bot.py --ablation-report
  ```
  Com várias contas, junte `--account STARTRADER-Demo_12345678`.
* Recomeçar a aprendizagem da equipa (pesos, calibração e banco por desempenho voltam ao início; operações, campeões e dossiers ficam):
  ```
  python mt5_bot.py --reset-team-weights
  ```
* Relatório HTML diário: gerado sozinho na mudança de dia em `reports\<servidor>_<login>\daily_AAAA-MM-DD.html` (em português, abre em qualquer navegador).

---

## 20. Resolução de problemas

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
| O painel não abre no navegador | O painel não está a correr, ou outra porta. | Veja a janela do supervisor ou `panel.log`. Arranque-o à mão com `start_panel.bat` e use o endereço que ele mostra. Com `"panel": {"enabled": false}` o supervisor não o arranca. |
| `Port 8765 is already in use` (código 1) | Outro painel (ou outro programa) já usa a porta. | Feche o outro painel ou use `start_panel.bat --port 8800` (ou `panel.port`). Para ver quem a usa: `netstat -ano \| findstr :8765`. |
| `panel.host is ... but MT5BOT_PANEL_TOKEN is not set` (código 2) | Acesso remoto sem token. | Prefira um túnel SSH (secção 12.6). Se precisar mesmo do modo remoto, defina o token (secção 18.1). |
| O painel diz "Ainda não há dados" ou "O bot não actualiza o ficheiro de saúde" | O bot não está a correr, ou o painel está a ver outra conta. | Arranque o bot. Com várias contas: `start_panel.bat --state-dir state\<servidor>_<login>`. |
| O painel mostra "a consultar" em vez de "ao vivo" | A ligação ao vivo foi cortada (proxy, antivírus, suspensão). | O painel passa a ler de 3 em 3 segundos e volta a ligar sozinho. Recarregue a página se persistir. |
| Botões de controlo não funcionam (`acções de controlo desligadas`) | `panel.control_actions_enabled` é `false` ou falta `MT5BOT_PANEL_TOKEN`. | Secção 12.5. Depois de `setx`, abra um **novo** `cmd` e reinicie o supervisor. |
| Conselheiro em `NO_KEY` | A variável não chegou ao processo do bot. | Um processo já a correr nunca vê variáveis definidas depois de arrancar. Defina `ANTHROPIC_API_KEY` com `setx`, feche **todas** as janelas `cmd` e arranque de novo; com o arranque automático, termine e volte a iniciar sessão no Windows. Alternativa sem reiniciar: `python mt5_bot.py --set-claude-key` (o bot procura no Gestor de Credenciais de 5 em 5 minutos). |
| Conselheiro em `OFFLINE_NO_CREDIT` | Crédito esgotado na conta Anthropic. | Carregue crédito na consola. O bot testa ao fim de 30 minutos, depois com intervalos que duplicam até 6 horas, e volta a `ONLINE` sozinho. Entretanto negoceia sem ele. |
| Conselheiro em `OFFLINE_AUTH` | Chave inválida, revogada ou sem permissão. | Crie uma chave nova e defina-a (secção 11.1). O bot só volta a tentar com uma chave **diferente**. |
| Conselheiro em `DEGRADED` | Sobrecarga, limite de pedidos ou rede da Anthropic. | Nada a fazer: tenta de novo com espera crescente. Entretanto negoceia sem ele. |
| Conselheiro em `BUDGET_CAPPED` | Atingiu `max_calls_per_hour` ou `max_daily_cost_usd`. | Comportamento pretendido. Retoma na janela seguinte. Suba os limites só se fizer sentido para si. |
| Conselheiro em `DISABLED` com `enabled: true` | Falta o pacote `anthropic`, ou correu com `--no-claude`. | `pip install anthropic` no ambiente virtual e reinicie. |
| Um agente aparece com erro ou "no banco" | Falhou repetidamente (banco por erros), ou não acrescentou valor fora da amostra (banco por desempenho). | A ficha do agente no painel diz o motivo. Por erros: veja o `bot.log` (`findstr /C:"agent" /C:"benched" bot.log`); volta sozinho depois da espera. Por desempenho: é o comportamento pretendido; volta quando a evidência mudar. |
| Agentes parados há muito tempo | Mercado fechado, fora de `trading_hours_server`, sem campeão (não há propostas, logo não há reuniões), ou o bot parou. | Veja **Saúde do sistema** no painel e `python mt5_bot.py --status`. |
| A equipa nunca sai de `baseline` em `auto` | Ainda não há decisões resolvidas suficientes, ou a equipa não supera o núcleo base. | Normal: o separador **Equipa** mostra "a recolher evidência (n/50)" ou o veredicto. É a protecção a funcionar. |
| Sem operações e o dossier diz "data unhealthy" | A Escriturária de Dados detectou velas em falta, preços parados ou ticks antigos. | Veja o motivo no dossier. Confirme a ligação e o histórico no MT5. As entradas voltam sozinhas quando os dados estiverem bons. |
| `Guardian froze new entries` | Uma invariante falhou (secção 10.3). | Leia o motivo no `bot.log` ou no painel. Corrija a causa (por exemplo, uma posição aberta à mão com o `magic` do bot). Levanta-se sozinho depois de 3 verificações limpas. |

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

## 21. Executar os testes

Os testes substituem o pacote `MetaTrader5` por uma versão falsa e o conselheiro por um cliente falso, por isso **nunca** tocam no terminal, na conta nem na API da Anthropic (não precisam de chave nem de rede), e correm também em Linux ou macOS.

```
cd /d C:\mt5-bot
.venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
```

**Sucesso:** a última linha diz `685 passed, 6 skipped in ...` (cerca de 2 a 4 minutos). Os ignorados são os testes lentos e os que precisam do pacote opcional `anthropic`. Os testes marcados como lentos (a optimização completa em muitas sementes de passeio aleatório e a simulação ponta a ponta da v2) correm com:

```
pytest -q --runslow
```

**Sucesso:** `689 passed, 2 skipped in ...` (cerca de 7 a 10 minutos; os 2 ignorados precisam do pacote opcional `anthropic`).

O que é verificado, entre outros:

* **v2:** política de contas (demo, real, confirmação, código 3, pastas separadas), tectos de risco e todos os comportamentos proibidos, disjuntor de drawdown e `--resume`, ausência de lookahead em todas as estratégias, P&L calculado à mão com spread, comissão e a regra pessimista, **NoTrade em dados de passeio aleatório em praticamente todas as sementes**, descoberta de uma vantagem injectada, promoção com sombra, histerese e cooldown, despromoção, reconciliação e reparação de SL, supervisor e o seu disjuntor, escrita atómica e recarga do `config.json`.
* **Conselheiro (`test_advisor.py`, `test_authority.py`):** classificação de todos os erros da API, máquina de estados e esperas, orçamento por hora e por dia (reposto depois de um reinício), esquema estrito, cache, recusas, respostas lentas, recuperação depois de uma falha, a garantia "falha = mesma decisão que sem conselheiro" (secção 11.4), conselheiro aleatório que nunca aumenta o tamanho, injecção de instruções em notícias e ausência da chave em registos, base de dados e tracebacks. Com o pacote `anthropic` instalado, `test_advisor_sdk.py` verifica também o formato dos pedidos com o SDK verdadeiro contra um servidor local (sem rede).
* **Equipa (`test_team_*.py`, `test_research.py`):** barramento de eventos com muitos threads, isolamento e tempo limite dos agentes, agregação e os seus limites, veto final do risco, bloqueios de segurança em todos os modos, congelamento e kill switch do Guardião, pesos, calibração, meta-etiquetadora validada, ablação e a escolha do modo `auto`, e a integração com o bot (entradas, gestão de posições que só aperta, pausa pelo painel).
* **Painel (`test_panel.py`):** todos os endpoints respondem com cabeçalhos de segurança, nenhum segredo ou identificador sai, ficheiros estáticos sem fuga de pastas, fluxo ao vivo (entrega e retoma), acções de controlo (token, CSRF, origem, confirmação, limite e auditoria), regras do acesso remoto, tradução de eventos em personagens, janelas de tempo da simulação, o painel não importa o ciclo de negociação, matar o painel não afecta o bot, o supervisor reinicia o painel e desiste dele com segurança, e nenhum código gerado em tempo de execução.

### 21.1 Teste do painel no navegador

O Playwright **não** faz parte do projecto (seria uma dependência grande só para testes). Em vez disso, `tests\frontend_smoke.mjs` usa o Chrome sem janela através do protocolo DevTools, só com o Node.js 22 ou mais recente e sem pacotes npm. Com o painel em demonstração numa janela (`start_panel.bat --demo`), noutra:

```
node tests\frontend_smoke.mjs http://127.0.0.1:8765/ smoke "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

**Sucesso:** termina com código 0 e grava em `smoke\` as capturas do escritório, de cada separador, da ficha de um agente, do tema claro e do ecrã de telemóvel, e um `report.json` sem erros. Falha se houver qualquer erro de JavaScript, um separador sem conteúdo, a ficha não abrir ou a página tiver deslocamento horizontal no telemóvel.

**Lista de verificação manual** (se não tiver Node.js ou Chrome), com `start_panel.bat --demo`:

1. A página abre com o título "CYBERBOX Trading Office" e a faixa roxa SIMULAÇÃO.
2. No escritório, as personagens mexem-se, falam (balões) e reúnem-se na Sala do Conselho; o cartão da reunião mostra votos e resultado.
3. Clicar numa personagem abre a ficha; Esc fecha-a.
4. O fluxo de eventos actualiza-se; os filtros funcionam.
5. A repetição: arrastar a barra e carregar em **Reproduzir** revê o dia; **Ao vivo** volta ao presente.
6. Os 10 separadores abrem sem mensagens de erro; os botões **Tabela** e **CSV** funcionam.
7. Numa linha de **Trades** ou **Registo de decisões**, o dossier abre.
8. **Tema** alterna claro e escuro; **EN** muda a língua.
9. Em **Saúde do sistema**, **Pausar novas entradas** com o token mostrado na janela da demonstração cria a pausa e aparece na auditoria; **Retomar entradas** retira-a.
10. Com a janela estreita (telemóvel), não há deslocamento horizontal da página.

---

## 22. Limitações e pressupostos

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

**Não verificado na v3** (desenvolvida e testada em Linux, com um MT5 simulado e um cliente da Anthropic falso):

* a API verdadeira da Anthropic: nenhuma chamada real foi feita. O formato dos pedidos foi verificado com o SDK oficial `anthropic` (versão 2.3.0) contra um servidor local; nomes de modelos, preços, parâmetros aceites (por exemplo `effort`, `output_config` e o mecanismo de recurso em caso de recusa) e mensagens de erro reais podem mudar. Se a Anthropic mudar algo, o pior caso é o conselheiro ficar indisponível, e o bot continua sem ele;
* o painel no Windows: testado no Chrome em Linux (incluindo tema claro e ecrã de telemóvel), não no Edge nem no Firefox, nem com o servidor do painel a correr em Windows;
* o `PanelKeeper` do supervisor, `start_panel.bat` e o `-NoPanel` do `install_autostart.ps1` em Windows reais;
* o Gestor de Credenciais do Windows através do pacote `keyring` (testado com um substituto);
* a equipa com dados reais de uma corretora: os pesos, a ablação e a escolha do modo `auto` só foram exercitados em simulação e em testes, onde nada prova valor em mercados reais.

**Pressupostos e escolhas da v3:**

* **Omissões de orçamento** (20 chamadas por hora, 0,50 USD por dia, 1500 ms por revisão, 10 s por pedido): chegam para rever todas as entradas de um bot em M15 com poucos símbolos e mais as tarefas diárias, e limitam o custo de uma avaria a cêntimos por dia. São estimativas locais; o limite da consola da Anthropic é o que conta.
* **Omissões da equipa** (pesos entre 0,25 e 2,0, taxa 0,05, decaimento 0,01, 30 resultados de aquecimento): aprender devagar e com limites, para que nenhum agente domine por sorte num período curto.
* **Portões da agregação** (confiança mínima 0,52, tamanho completo a partir de 0,65, desacordo máximo 0,5): exigem uma maioria informada; com sinais fracos ou divididos o tamanho cai ou não há trade. Os portões rígidos da v2 (walk-forward, sombra, Sharpe deflacionado, camada de risco) ficaram todos iguais.
* **`team_mode: auto` começa em `baseline`:** a equipa tem de provar o seu valor fora da amostra antes de mudar uma única decisão. Até lá o bot é a v2, e o conselheiro (se ligado) é consultado e medido mas não altera o volume.
* **Temperatura:** não é enviada, porque os modelos actuais não a aceitam (secção 11.5).
* O resultado de uma proposta recusada é medido pela operação em sombra do campeão na vela seguinte (as regras do backtest), não por uma operação real.
* Os motivos técnicos escritos pelo núcleo (por exemplo os da camada de risco, nos dossiers) aparecem em inglês, tal como no `bot.log`; o painel traduz os mais comuns.
* A simulação (`--simulate`, `--demo`) coloca campeões de propósito e salta a optimização walk-forward (precisaria de meses de histórico); serve para ver o funcionamento, não para avaliar estratégias.

**Limitações da v3:**

* A equipa e o conselheiro não criam vantagem: só podem reduzir o risco de uma proposta que já passou os portões da v2. Podem também recusar operações que teriam sido lucrativas.
* Os custos da API da Anthropic são da sua responsabilidade.
* O painel mostra dados com alguns segundos de atraso e não substitui o terminal MT5 para ver o estado real da conta.
