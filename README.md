# Bot de negociação para MetaTrader 5 (Python, Windows)

Guia completo de instalação e operação do `mt5_bot.py`, um bot em Python que negoceia automaticamente através do terminal MetaTrader 5 (MT5) instalado no seu PC Windows. Foi pensado para a corretora STARTRADER, mas funciona com qualquer corretora MT5.

## Índice

1. [O que é isto](#1-o-que-é-isto)
2. [Requisitos](#2-requisitos)
3. [Instalar o Python](#3-instalar-o-python)
4. [Instalar o MT5 e entrar numa conta DEMO](#4-instalar-o-mt5-e-entrar-numa-conta-demo)
5. [Preparar o MT5 para automação](#5-preparar-o-mt5-para-automação)
6. [Instalar o projecto](#6-instalar-o-projecto)
7. [Configurar o config.json](#7-configurar-o-configjson)
8. [Primeira execução em modo dry-run](#8-primeira-execução-em-modo-dry-run)
9. [Executar em demo com ordens reais](#9-executar-em-demo-com-ordens-reais)
10. [Parar o bot](#10-parar-o-bot)
11. [Execução sem supervisão](#11-execução-sem-supervisão)
12. [Passar a conta real (só depois de validar)](#12-passar-a-conta-real-só-depois-de-validar)
13. [Credenciais por variáveis de ambiente](#13-credenciais-por-variáveis-de-ambiente)
14. [Substituir a estratégia](#14-substituir-a-estratégia)
15. [Resolução de problemas](#15-resolução-de-problemas)
16. [Executar os testes](#16-executar-os-testes)
17. [Pressupostos e limitações](#17-pressupostos-e-limitações)

---

## 1. O que é isto

Um único ficheiro Python (`mt5_bot.py`) que:

* liga-se ao terminal MT5 aberto no seu PC, através do pacote oficial `MetaTrader5`;
* lê as velas (barras) já fechadas de cada símbolo configurado;
* pede um sinal a uma função de estratégia substituível (`generate_signal`);
* calcula o volume (lotes) para que a perda no stop loss seja uma percentagem fixa do capital;
* envia ordens a mercado **sempre com stop loss (SL) e take profit (TP)** no servidor da corretora;
* aplica protecções: limite de perda diária, interruptor de emergência (kill switch), filtro de spread, horário de negociação, número máximo de posições, reconexão automática e recusa de contas reais por omissão.

> ### AVISO DE RISCO (leia antes de continuar)
>
> * **A maioria dos investidores particulares que negoceiam CFD e forex perde dinheiro.** As próprias corretoras publicam percentagens de contas perdedoras, normalmente entre 70% e 80%.
> * **A estratégia de exemplo (cruzamento de médias móveis EMA) NÃO tem vantagem comprovada.** Existe apenas para demonstrar a infra-estrutura. Não espere lucro dela.
> * **Comece sempre numa conta DEMO.** O bot recusa arrancar numa conta real, a menos que active essa opção explicitamente.
> * Automação não elimina riscos: falhas de internet, de energia, da corretora ou do próprio código podem causar perdas. O SL protege cada posição no servidor, mas pode haver deslizamento (slippage) e saltos de preço (gaps).
> * Use apenas dinheiro que pode perder na totalidade. Este software é fornecido sem qualquer garantia.

---

## 2. Requisitos

| Requisito | Detalhe |
|---|---|
| Sistema operativo | Windows 10 ou 11, 64 bits. O pacote `MetaTrader5` só funciona em Windows. |
| Python | 3.10, 3.11 ou 3.12, **64 bits**. Recomendado: 3.12. |
| MetaTrader 5 | O terminal MT5 da STARTRADER, transferido do site oficial ou da Área de Cliente da corretora. |
| Conta | Uma conta DEMO para começar (login, palavra-passe e nome do servidor). |
| PC | Ligado em permanência enquanto o bot negoceia, com o terminal MT5 aberto e com sessão iniciada. |

Verifique o Python (depois de o instalar, ver secção 3) numa janela `cmd`:

1. Liste as versões instaladas e os caminhos:
   ```
   py -0p
   ```
   **Sucesso:** aparece uma linha como `-V:3.12 *        C:\Users\SeuNome\AppData\Local\Programs\Python\Python312\python.exe`. Se aparecer `-32` no fim da versão (por exemplo `-V:3.12-32`), essa instalação é de 32 bits e não serve.
2. Confirme que o `python` activo é de 64 bits:
   ```
   python -c "import struct; print(struct.calcsize('P')*8)"
   ```
   **Sucesso:** imprime `64`.

---

## 3. Instalar o Python

1. Abra https://www.python.org/downloads/windows/ e, na secção "Stable Releases", escolha a versão 3.12 mais recente e clique em **Windows installer (64-bit)**.
2. Execute o instalador. No primeiro ecrã, **marque a caixa "Add python.exe to PATH"** (fica na parte de baixo da janela).
3. Clique em **Install Now** e aguarde até aparecer "Setup was successful". Clique em **Close**.
4. Abra uma **nova** janela `cmd` (tecla Windows, escreva `cmd`, Enter). As janelas abertas antes da instalação não vêem o novo PATH.
5. Verifique:
   ```
   py --version
   ```
   **Sucesso:** imprime `Python 3.12.x`.
6. Verifique também o comando `python`:
   ```
   python --version
   ```
   **Sucesso:** imprime `Python 3.12.x`. Se em vez disso abrir a Microsoft Store, vá a **Definições > Aplicações > Definições avançadas de aplicações > Aliases de execução de aplicações** e desligue `python.exe` e `python3.exe`. Feche e reabra o `cmd`.
7. Faça as duas verificações de 64 bits da secção 2.

---

## 4. Instalar o MT5 e entrar numa conta DEMO

1. Transfira o MetaTrader 5 do site oficial da STARTRADER ou da sua Área de Cliente (não use cópias de outros sites). Execute o instalador e aceite as opções por omissão.
   **Sucesso:** o MT5 abre e mostra gráficos.
2. Se ainda não tem conta demo, crie-a na Área de Cliente da STARTRADER ou no próprio MT5 em **File > Open an Account**. Guarde o **login** (número), a **palavra-passe** e o **nome do servidor** que lhe forem atribuídos.
3. No MT5 abra **File > Login to Trade Account**. Preencha:
   * **Login:** o número da conta;
   * **Password:** a palavra-passe de negociação (não a de investidor, que é só de leitura);
   * **Server:** escolha o servidor da lista (normalmente o nome contém `Demo`);
   * marque **Save password**, para o terminal voltar a entrar sozinho depois de reiniciar.

   Clique em **OK**.
4. Confirme que a ligação está activa:
   * no canto inferior direito da janela do MT5 aparece um indicador de ligação com barras verdes e o tempo de resposta (ping) em milissegundos. Se aparecer `No connection` ou `Invalid account`, a ligação falhou;
   * a barra de título da janela mostra o número da conta, o nome do servidor e `Demo Account`;
   * em **View > Navigator** (Ctrl+N), na pasta **Accounts**, a conta aparece com o ícone activo.
5. Para saber o nome exacto do servidor: está no campo **Server** da janela de login, na barra de título do MT5, e também aparece no resultado do teste de ligação da secção 6.

> **Dica:** se o MT5 estiver em português, os nomes dos menus mudam. Para seguir este guia à letra, mude a língua em **View > Languages > English** e reinicie o terminal.

---

## 5. Preparar o MT5 para automação

1. Abra **Tools > Options**, separador **Expert Advisors**:
   * marque **Allow algorithmic trading**;
   * se existir a opção **Disable algorithmic trading via external Python API**, **desmarque-a** (se estiver marcada, o Python não consegue negociar);
   * recomendado: desmarque **Disable algorithmic trading when the account has been changed** se pretende trocar de conta sem ter de voltar a ligar o Algo Trading (o bot verifica a conta por si).

   Clique em **OK**.
2. Na barra de ferramentas, clique no botão **Algo Trading** (atalho Ctrl+E) até ficar **verde**. Vermelho significa desligado.
   **Sucesso:** o botão está verde. Este estado é guardado e mantém-se depois de reiniciar o MT5.
3. Abra **View > Market Watch** (Ctrl+M). Clique com o botão direito na lista e escolha **Show All** (ou **Symbols**, Ctrl+U, para pesquisar).
4. Encontre o nome **exacto** do símbolo que quer negociar, incluindo sufixos. Exemplos: `EURUSD`, `EURUSD.m`, `EURUSD+`, `XAUUSD.s`. É esse texto, letra a letra, que vai em `symbols` no `config.json`.
5. Clique com o botão direito no símbolo e escolha **Specification**. Aqui vê o tamanho do contrato, o volume mínimo, o passo de volume, o `Stops level` e os modos de execução. Útil para perceber porque é que uma ordem foi recusada.
6. Leia a **hora do servidor**: o cabeçalho da janela Market Watch mostra a hora actual do servidor da corretora (por exemplo `Market Watch: 14:32:05`). Compare com a hora do seu PC para saber a diferença. A opção `trading_hours_server` usa **esta** hora, não a hora local.
7. Confirme em **Tools > Options > Charts** que **Max bars in chart** é maior que o valor de `bars` no `config.json` (300 por omissão). `Unlimited` serve.

---

## 6. Instalar o projecto

1. Crie a pasta do projecto num `cmd`:
   ```
   mkdir C:\mt5-bot
   ```
2. Copie para `C:\mt5-bot` os ficheiros `mt5_bot.py`, `config.json`, `requirements.txt`, `requirements-dev.txt`, `start_bot.bat`, `README.md` e a pasta `tests`.
3. Entre na pasta:
   ```
   cd /d C:\mt5-bot
   ```
   **Sucesso:** o cursor mostra `C:\mt5-bot>`.
4. Crie o ambiente virtual (isola as bibliotecas deste projecto):
   ```
   py -3.12 -m venv .venv
   ```
   **Sucesso:** aparece a pasta `C:\mt5-bot\.venv`. Se usa outra versão suportada, troque `3.12` por `3.10` ou `3.11`.
5. Active o ambiente virtual:
   ```
   .venv\Scripts\activate
   ```
   **Sucesso:** o cursor passa a `(.venv) C:\mt5-bot>`. Tem de activar o ambiente sempre que abrir um novo `cmd` para usar o bot à mão.
6. Instale as dependências:
   ```
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
   **Sucesso:** a última linha diz `Successfully installed MetaTrader5-... numpy-...`.
7. Com o MT5 **aberto e com sessão iniciada**, faça o teste de ligação:
   ```
   python -c "import MetaTrader5 as m; print(m.initialize(), m.account_info())"
   ```
   **Sucesso:** imprime `True AccountInfo(login=..., trade_mode=0, ...)`. `trade_mode=0` significa conta demo; `2` significa conta real. Se imprimir `False None`, veja o erro com:
   ```
   python -c "import MetaTrader5 as m; print(m.initialize(), m.last_error())"
   ```
   e consulte a secção 15.
8. Confirme que o terminal permite negociação algorítmica:
   ```
   python -c "import MetaTrader5 as m; m.initialize(); print(m.terminal_info().trade_allowed)"
   ```
   **Sucesso:** imprime `True`. Se imprimir `False`, volte à secção 5, passo 2.

---

## 7. Configurar o config.json

Abra `C:\mt5-bot\config.json` num editor de texto (Bloco de Notas serve). Regras do formato JSON: texto entre aspas duplas, `true`/`false`/`null` em minúsculas, sem vírgula depois do último item, e em caminhos do Windows cada `\` escreve-se `\\`.

O bot valida tudo ao arrancar. Chaves desconhecidas (por exemplo um erro de escrita como `"symbol"`) são recusadas com uma mensagem a indicar o problema.

| Chave | Significado | Predefinição segura | Intervalo recomendado |
|---|---|---|---|
| `symbols` | Lista de símbolos a negociar, com o nome exacto do Market Watch. | `["EURUSD"]` | 1 a 5 símbolos líquidos |
| `timeframe` | Período das velas: `M1`, `M5`, `M15`, `M30`, `H1`, `H4`. | `"M15"` | `M15` a `H4` (períodos curtos sofrem mais com o spread) |
| `bars` | Número de velas fechadas enviadas à estratégia. Mínimo: `3 * max(slow_ema, atr_period) + 2`. | `300` | 200 a 1000 |
| `magic` | Número que identifica as ordens deste bot. O bot só mexe em posições com este número. | `510001` | Qualquer inteiro positivo, único por instância do bot |
| `risk_per_trade_pct` | Percentagem do capital (equity) arriscada até ao SL em cada operação. | `0.5` | 0.25 a 1 (máximo aceite: 2) |
| `max_daily_loss_pct` | Perda diária máxima, em % do capital no início do dia do servidor. Ao atingir, fecha as posições do bot e pára até ao dia seguinte. | `2.0` | 1 a 3 (máximo aceite: 10) |
| `max_open_positions_total` | Máximo de posições do bot abertas em simultâneo, em todos os símbolos. | `3` | 1 a 5 |
| `max_spread_points` | Spread máximo, em pontos, para abrir uma posição. Em EURUSD com 5 casas decimais, 10 pontos = 1 pip. | `20` | 1,5 a 3 vezes o spread típico do símbolo |
| `deviation_points` | Desvio máximo de preço aceite na execução, em pontos. Entra no cálculo do volume (pior preço possível). | `10` | 5 a 30 |
| `trading_hours_server` | Janela `[início, fim)` em horas do servidor. `[7, 20]` = das 07:00 às 19:59. Aceita janelas que atravessam a meia-noite, como `[22, 6]`. `[0, 24]` = sempre. | `[7, 20]` | Evite a hora de rollover (por volta das 00:00 do servidor), quando o spread alarga |
| `fast_ema` | Período da EMA rápida (estratégia de exemplo). | `20` | 5 a 50, menor que `slow_ema` |
| `slow_ema` | Período da EMA lenta (estratégia de exemplo). | `50` | 20 a 200 |
| `atr_period` | Período do ATR (Wilder), usado para SL e TP. | `14` | 10 a 30 |
| `sl_atr_mult` | Distância do SL = ATR x este valor. | `2.0` | 1 a 4 |
| `tp_atr_mult` | Distância do TP = ATR x este valor. | `3.0` | 1 a 6 |
| `close_on_opposite` | Se `true`, um sinal contrário fecha a posição do bot nesse símbolo (e abre a nova, se os filtros deixarem). | `true` | `true` ou `false` |
| `allow_live` | Permite arrancar numa conta real. | `false` | Mantenha `false` até concluir a secção 12 |
| `dry_run` | Faz tudo, incluindo `order_check`, mas nunca envia ordens. Equivale a `--dry-run`. | `false` | `true` nos primeiros dias |
| `poll_seconds` | Intervalo entre ciclos do bot, em segundos. | `5` | 2 a 30 |
| `heartbeat_minutes` | Intervalo entre mensagens de estado (heartbeat) no registo. | `30` | 5 a 60 |
| `kill_switch_file` | Nome do ficheiro que, se existir na pasta do bot, fecha as posições do bot e termina. | `"KILL"` | Manter |
| `state_file` | Ficheiro JSON com o capital de início do dia e o estado de paragem. | `"bot_state.json"` | Manter |
| `log_file` | Ficheiro de registo (rotativo: 2 MB x 5 ficheiros). | `"bot.log"` | Manter |
| `mt5_path` | Caminho completo para `terminal64.exe`. Só é necessário se tiver vários terminais MT5 instalados. | `null` | Exemplo: `"C:\\Program Files\\NomeDaPasta\\terminal64.exe"` |

Para encontrar o caminho do `terminal64.exe`: clique com o botão direito no atalho do MT5, escolha **Propriedades** e copie o campo **Destino**.

---

## 8. Primeira execução em modo dry-run

No modo dry-run o bot faz todo o trabalho (ligação, verificações, sinais, cálculo do volume e `order_check` na corretora), mas **nunca envia ordens**.

1. Com o MT5 aberto, ligado e com o Algo Trading verde, abra um `cmd`:
   ```
   cd /d C:\mt5-bot
   .venv\Scripts\activate
   python mt5_bot.py --dry-run
   ```
2. **Sucesso:** aparecem linhas como estas (valores ilustrativos):
   ```
   2025-10-13 09:00:01,102 INFO     mt5_bot 1.0.0 starting in DRY RUN mode
   2025-10-13 09:00:01,103 INFO     Connecting to MT5 terminal (path=auto-detect, credentials from environment=no)
   2025-10-13 09:00:01,220 INFO     Connected: account ***321 on NomeDoServidor-Demo (DEMO, USD), equity 10000.00 USD, mode=DRY RUN (no orders will be sent)
   2025-10-13 09:00:01,221 INFO     Config: symbols=EURUSD timeframe=M15 risk=0.50% daily_loss=2.00% magic=510001
   2025-10-13 09:00:01,222 INFO     Bot running (poll every 5s). Create the file KILL to stop.
   2025-10-13 09:00:01,230 INFO     New server day 2025-10-13: start equity 10000.00
   2025-10-13 09:00:01,231 INFO     Heartbeat: equity 10000.00 USD, balance 10000.00, bot positions 0, halted=False, day start equity 10000.00
   2025-10-13 09:00:01,240 INFO     EURUSD: last closed bar 2025-10-13 09:45 server recorded; waiting for the next one
   ```
   e, quando surgir um sinal:
   ```
   2025-10-13 13:00:03,511 INFO     EURUSD: BUY signal on bar 2025-10-13 12:45 server (ATR 0.001200)
   2025-10-13 13:00:03,530 INFO     DRY RUN: would send BUY EURUSD 0.20 lots at 1.1001 SL 1.09771 TP 1.10369 (order_check OK)
   ```
   O número de conta aparece mascarado (`***321`) de propósito. Na primeira volta o bot só regista a última vela fechada e espera pela seguinte; é normal não acontecer nada durante um período de vela (15 minutos em M15).
3. Deixe-o a correr **pelo menos 2 a 3 dias de mercado** (de segunda a sexta). O cruzamento de médias é raro: pode haver só alguns sinais por semana.
4. Verifique o `bot.log` (fica em `C:\mt5-bot`). Noutro `cmd`:
   * ver as ordens que teriam sido enviadas:
     ```
     findstr /C:"DRY RUN" bot.log
     ```
   * ver avisos e erros:
     ```
     findstr /C:"WARNING" /C:"ERROR" /C:"CRITICAL" bot.log
     ```
   * acompanhar o registo em tempo real:
     ```
     powershell -Command "Get-Content bot.log -Wait -Tail 30"
     ```
5. O que procurar:
   * linhas `Heartbeat` com a regularidade configurada (o bot está vivo);
   * linhas `DRY RUN: would send ... (order_check OK)`: a corretora aceitaria a ordem;
   * linhas `order_check rejected the order: retcode=...`: há um problema a corrigir (ver secção 15);
   * linhas `spread ... > max` ou `outside trading window`: os filtros estão a funcionar; se surgirem sempre, ajuste `max_spread_points` ou `trading_hours_server`;
   * nenhuma linha `ERROR` ou `Traceback` sem explicação.
6. Pare com **Ctrl+C**.

---

## 9. Executar em demo com ordens reais

1. Confirme que `"dry_run": false` no `config.json` e que está numa conta **demo**.
2. Arranque sem `--dry-run`:
   ```
   cd /d C:\mt5-bot
   .venv\Scripts\activate
   python mt5_bot.py
   ```
   **Sucesso:** a linha `Connected:` termina em `mode=LIVE ORDERS` e mostra `(DEMO, ...)`. Quando houver uma entrada aparece:
   ```
   EURUSD: opened BUY 0.20 lots at 1.1001 SL 1.09771 TP 1.10369 (order ..., deal ...)
   ```
3. Ver as operações do bot no MT5:
   * **View > Toolbox** (Ctrl+T), separador **Trade**: posições abertas. Confirme que as colunas **S / L** e **T / P** estão preenchidas e que o comentário é `mt5_bot` (se a coluna Comment não estiver visível, clique com o botão direito no cabeçalho da tabela e active-a). A corretora pode alterar o comentário, por exemplo quando o SL é atingido.
   * Separador **History**: clique com o botão direito, escolha **Positions** para ver posições completas e escolha o período (por exemplo **Last 3 Months**). No mesmo menu, **Report** exporta um relatório (as opções podem variar com a versão do terminal).
   * O MT5 não mostra o número magic nas tabelas. Para listar só as posições abertas deste bot:
     ```
     python -c "import MetaTrader5 as m; m.initialize(); print([(p.ticket, p.symbol, p.volume, p.sl, p.tp) for p in (m.positions_get() or []) if p.magic == 510001])"
     ```
   * Resultado líquido acumulado das operações do bot (lucro + comissões + swaps):
     ```
     python -c "import MetaTrader5 as m, datetime as d; m.initialize(); ds = m.history_deals_get(d.datetime(2020, 1, 1), d.datetime.now() + d.timedelta(days=2)) or []; print(round(sum(x.profit + x.commission + x.swap for x in ds if x.magic == 510001), 2))"
     ```
     Troque `510001` pelo seu `magic` se o alterou. Compare o resultado com o separador History.
4. **Validação recomendada: 2 a 3 meses em demo**, sem mexer nos parâmetros a meio. Registe semanalmente:
   * número de operações (com menos de 50 a 100 operações as conclusões são fracas);
   * taxa de acerto e relação ganho médio / perda média;
   * profit factor (ganhos brutos / perdas brutas) e expectativa por operação;
   * drawdown máximo e maior sequência de perdas;
   * custos: spread, comissões e swaps;
   * deslizamento: diferença entre o preço pedido e o executado;
   * dias em que o limite diário foi atingido, desconexões e erros no `bot.log`;
   * comparação com o que esperava de um backtest feito antes.

---

## 10. Parar o bot

| Método | Como | O que acontece às posições |
|---|---|---|
| **Ctrl+C** (ou Ctrl+Break) | Na janela do bot. | O bot termina de forma ordenada (`Stop requested ...`). As posições **ficam abertas**, protegidas pelo SL e TP no servidor. |
| **Kill switch** | Noutro `cmd`: `cd /d C:\mt5-bot` e depois `type nul > KILL` | Em poucos segundos (até `poll_seconds`) o bot **fecha todas as posições com o seu magic** (não toca nas outras) e termina com código 0. |
| Fechar a janela (X) ou desligar o PC | Não recomendado. | O processo é terminado à força. As posições ficam abertas com SL e TP no servidor. |

Depois de usar o kill switch, o ficheiro `KILL` **fica na pasta de propósito**: enquanto existir, o bot fecha as posições e sai sempre que arrancar (útil se tiver o arranque automático configurado). Para voltar a usar o bot:
```
cd /d C:\mt5-bot
del KILL
```

Com o bot parado, as posições abertas continuam protegidas pelo SL e TP, mas deixam de ter fecho por sinal contrário e o limite de perda diária deixa de ser vigiado.

---

## 11. Execução sem supervisão

### 11.1 Testar o start_bot.bat

1. Faça duplo clique em `C:\mt5-bot\start_bot.bat`.
   **Sucesso:** abre uma janela preta com as mesmas linhas de registo da secção 8. O `.bat` entra na pasta, activa o `.venv` e arranca o bot. Se o bot terminar com erro, a janela fica aberta com `Press any key to continue . . .` para poder ler a mensagem.
2. Pode passar opções, por exemplo num `cmd`: `C:\mt5-bot\start_bot.bat --dry-run`.

### 11.2 Abrir o MT5 ao iniciar sessão no Windows

1. Prima **Windows+R**, escreva `shell:startup` e prima Enter. Abre a pasta de arranque.
2. Noutra janela do Explorador, encontre o atalho do MT5 (por exemplo no ambiente de trabalho), clique com o botão direito, **Copiar**, e na pasta de arranque clique com o botão direito, **Colar atalho**.
3. Garanta que marcou **Save password** no login (secção 4) e que o botão Algo Trading fica verde ao reabrir.

### 11.3 Agendador de Tarefas (arranque do bot ao iniciar sessão)

1. Prima **Windows+R**, escreva `taskschd.msc` e prima Enter.
2. No painel direito, clique em **Create Task...** (não use "Create Basic Task").
3. Separador **General**:
   * **Name:** `MT5 Bot`;
   * escolha **Run only when user is logged on** (o MT5 corre na sua sessão do Windows e o bot precisa de falar com ele).
4. Separador **Triggers** > **New...**:
   * **Begin the task:** `At log on`;
   * **Specific user:** o seu utilizador;
   * em **Advanced settings**, marque **Delay task for:** e escolha `1 minute` (dá tempo ao MT5 para abrir e ligar; aumente para 2 ou 5 minutos se o arranque falhar);
   * confirme que **Enabled** está marcado. **OK**.
5. Separador **Actions** > **New...**:
   * **Action:** `Start a program`;
   * **Program/script:** `C:\mt5-bot\start_bot.bat`
   * **Start in (optional):** `C:\mt5-bot` (sem aspas e sem barra no fim). **OK**.
6. Separador **Conditions**: desmarque **Start the task only if the computer is on AC power**.
7. Separador **Settings**:
   * desmarque **Stop the task if it runs longer than:**;
   * em **If the task is already running, then the following rule applies:** escolha `Do not start a new instance`. **OK**.
8. Teste: na lista de tarefas, clique com o botão direito em **MT5 Bot** > **Run**.
   **Sucesso:** abre a janela do bot com o registo normal.

O Agendador só arranca a tarefa quando inicia sessão. Se o PC reiniciar sozinho (por exemplo por uma actualização), o bot só volta depois de iniciar sessão. O início de sessão automático no Windows (`netplwiz`) resolve isso, mas reduz a segurança do PC: qualquer pessoa com acesso físico entra na sua conta. Decida conscientemente.

### 11.4 Impedir suspensão e hibernação

1. **Definições > Sistema > Energia e bateria** (Windows 11) ou **Definições > Sistema > Energia e suspensão** (Windows 10): em suspensão, quando ligado à corrente, escolha **Nunca**. Desligar só o ecrã não faz mal.
2. Ou, num `cmd` aberto como administrador (botão direito em **Linha de Comandos** > **Executar como administrador**):
   ```
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /hibernate off
   ```
   **Sucesso:** os comandos terminam sem mensagens.

### 11.5 Windows Update

1. **Definições > Windows Update > Opções avançadas > Horas de atividade:** escolha **Manualmente** e cubra o seu horário de negociação (máximo 18 horas). O Windows evita reiniciar dentro dessa janela.
2. Antes de períodos importantes, pode usar **Pausar atualizações**. Não desligue as actualizações de segurança por tempo indefinido.

### 11.6 Energia e internet

* Use uma **UPS** (unidade de alimentação ininterrupta) para o PC e o router: cortes de energia são frequentes e um corte desliga tudo.
* Tenha uma **internet de reserva** (por exemplo um router 4G com comutação automática, ou partilha de dados do telemóvel). O bot reconecta-se sozinho, com espera crescente de 5 s até 120 s.
* Mesmo assim, verifique o `bot.log` e o MT5 pelo menos uma vez por dia. Em alternativa, considere um servidor virtual Windows (VPS) perto do servidor da corretora.

---

## 12. Passar a conta real (só depois de validar)

Lista de verificação. Só avance se puder marcar todos os pontos:

- [ ] 2 a 3 meses em demo, com as métricas da secção 9 registadas e aceitáveis.
- [ ] Percebe o significado de todas as linhas do `bot.log` e não há erros por explicar.
- [ ] Testou o kill switch, o Ctrl+C e o limite de perda diária em demo.
- [ ] Testou um reinício do PC e uma falha de internet (desligar o router) e o bot recuperou.
- [ ] Sabe que spreads, comissões e execução numa conta real podem ser diferentes da demo.
- [ ] Usa na conta real o mesmo tipo de conta (e os mesmos nomes de símbolos) que validou em demo, ou corrigiu `symbols`.
- [ ] Vai usar apenas capital que pode perder na totalidade.

Passos:

1. Deposite um **montante pequeno**.
2. **Teste um levantamento cedo**: peça o levantamento de uma pequena parte, para confirmar que o processo funciona e quanto tempo demora, antes de aumentar o capital.
3. No `config.json`:
   * mude `"allow_live": false` para `"allow_live": true`;
   * **reduza o risco**: por exemplo `"risk_per_trade_pct": 0.25` e `"max_daily_loss_pct": 1.0`.
4. Entre na conta real no MT5 (**File > Login to Trade Account**).
5. Primeiro, um dia em dry-run na conta real:
   ```
   python mt5_bot.py --dry-run
   ```
   **Sucesso:** a linha `Connected:` mostra `(REAL, ...)` e `mode=DRY RUN`.
6. Depois, arranque normalmente (`python mt5_bot.py` ou `start_bot.bat`). Acompanhe de perto nas primeiras semanas.

Se o terminal mudar de conta enquanto o bot corre, o bot pára com o código 3 por segurança.

---

## 13. Credenciais por variáveis de ambiente

**Por omissão não precisa de nada disto:** o bot usa a conta que já tem sessão iniciada no terminal. As variáveis de ambiente servem para o bot iniciar sessão numa conta específica.

1. Num `cmd` (substitua os valores; mantenha as aspas):
   ```
   setx MT5_LOGIN "12345678"
   setx MT5_PASSWORD "a-sua-palavra-passe"
   setx MT5_SERVER "NomeDoServidor-Demo"
   ```
   **Sucesso:** cada comando responde `SUCCESS: Specified value was saved.` (ou `ÊXITO: ...` num Windows em português).
2. Feche e abra um **novo** `cmd` (o `setx` não altera a janela actual). Verifique, sem mostrar a palavra-passe:
   ```
   echo %MT5_LOGIN% %MT5_SERVER%
   ```
3. Tem de definir as **três** variáveis ou nenhuma. Se só existir parte delas, o bot recusa arrancar.
4. Para remover:
   ```
   reg delete HKCU\Environment /v MT5_LOGIN /f
   reg delete HKCU\Environment /v MT5_PASSWORD /f
   reg delete HKCU\Environment /v MT5_SERVER /f
   ```

Porque não pôr a palavra-passe no `config.json` ou noutro ficheiro do projecto: os ficheiros são copiados, enviados por e-mail, guardados em cópias de segurança e partilhados (por exemplo num repositório Git) com facilidade, e a palavra-passe vai junto. O bot nunca escreve a palavra-passe no registo e mascara o número de conta.

Limitações honestas: o `setx` guarda os valores **em texto simples** no registo do Windows do seu utilizador (`HKCU\Environment`), legível por qualquer programa que corra com a sua conta. Proteja a sua conta do Windows com palavra-passe. Se a palavra-passe da corretora tiver caracteres especiais do `cmd` (`%`, `^`, `&`, `"`, `<`, `>`, `|`), o `setx` pode gravá-la mal; nesse caso, prefira usar a sessão já iniciada no terminal.

---

## 14. Substituir a estratégia

O resto do bot não depende da estratégia: só chama a função `generate_signal` uma vez por cada nova vela fechada de cada símbolo.

### 14.1 Contrato

```python
def generate_signal(closed_bars: np.ndarray, cfg: Config) -> Optional[Signal]:
```

* **Entrada `closed_bars`:** array estruturado do numpy, devolvido pelo MT5, com **apenas velas fechadas**, da mais antiga para a mais recente (`closed_bars[-1]` é a última vela fechada). A vela em formação nunca é incluída. Tem até `bars` velas.
* **Entrada `cfg`:** a configuração (só de leitura).
* **Saída:** `Signal("BUY", atr)`, `Signal("SELL", atr)` ou `None`. `atr` é uma distância de referência **em preço** (por exemplo `0.0012` em EURUSD): o SL fica a `atr * sl_atr_mult` e o TP a `atr * tp_atr_mult` do preço de entrada. Tem de ser positivo.
* **Regras:** não envie ordens, não leia ficheiros nem a rede, não use dados futuros, seja rápida e determinística. O volume, o SL, o TP e todos os filtros são tratados pelo bot.

Campos de cada vela:

| Campo | Tipo | Significado |
|---|---|---|
| `time` | inteiro | Abertura da vela, em segundos, na hora do servidor da corretora |
| `open` | decimal | Preço de abertura (bid) |
| `high` | decimal | Máximo |
| `low` | decimal | Mínimo |
| `close` | decimal | Fecho |
| `tick_volume` | inteiro | Número de ticks na vela |
| `spread` | inteiro | Spread em pontos |
| `real_volume` | inteiro | Volume negociado real (em forex e CFD costuma ser 0) |

Acesso: `closed_bars["close"]` devolve um array com todos os fechos; `closed_bars["high"][-1]` é o máximo da última vela fechada.

### 14.2 Exemplo mínimo

Substitua a função `generate_signal` no `mt5_bot.py` por, por exemplo, um rompimento de 20 velas (também **sem vantagem comprovada**):

```python
def generate_signal(closed_bars: np.ndarray, cfg: Config) -> Optional[Signal]:
    """20-bar breakout. Example only: no proven edge."""
    lookback = 20
    if len(closed_bars) < max(lookback, cfg.atr_period) + 2:
        return None
    high = closed_bars["high"]
    low = closed_bars["low"]
    close = closed_bars["close"]
    last_atr = float(atr(high, low, close, cfg.atr_period)[-1])
    if not np.isfinite(last_atr) or last_atr <= 0:
        return None
    if close[-1] > high[-lookback - 1:-1].max():
        return Signal("BUY", last_atr)
    if close[-1] < low[-lookback - 1:-1].min():
        return Signal("SELL", last_atr)
    return None
```

Notas:

* As chaves `fast_ema` e `slow_ema` continuam a ser validadas mesmo que a sua estratégia não as use. Para novos parâmetros, acrescente campos à classe `Config` e regras em `validate()`.
* Os testes da estratégia de exemplo em `tests/test_bot.py` (classe `TestStrategy`) vão falhar; substitua-os por testes da sua estratégia.
* **Faça backtest antes de usar**, com dados fora da amostra (out-of-sample): optimize os parâmetros num período e avalie-os noutro período que não usou, incluindo spread, comissões e swaps. O Strategy Tester do MT5 só testa Expert Advisors em MQL5, não código Python; pode obter histórico com `mt5.copy_rates_range` e simular a sua função. Desconfie de resultados demasiado bons: geralmente indicam sobre-optimização (overfitting).

---

## 15. Resolução de problemas

| Sintoma / mensagem | Causa provável | Solução |
|---|---|---|
| `mt5.initialize() failed: (-10003, 'IPC initialize failed, ...')` | MT5 fechado ou ainda a arrancar; Python de 32 bits; vários terminais instalados; MT5 e Python a correr com permissões diferentes (um como administrador e o outro não). | Abra o MT5 e espere que ligue. Confirme 64 bits (secção 2). Defina `mt5_path`. Corra os dois com o mesmo nível de permissões. |
| `mt5.initialize() failed: (-6, 'Terminal: Authorization failed')` | Credenciais erradas nas variáveis de ambiente. | Corrija `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (secção 13) ou remova-as. |
| `Algo Trading is disabled in the terminal` | Botão Algo Trading vermelho ou opção desligada. | Secção 5, passos 1 e 2. |
| `Trading is not allowed on this account` | Sessão iniciada com a palavra-passe de investidor (só leitura). | Volte a entrar com a palavra-passe de negociação. |
| `Symbol 'EURUSD' not found` | O nome do símbolo tem sufixo ou não existe nessa conta. | Secção 5, passos 3 e 4; copie o nome exacto para `symbols`. |
| `Refusing to trade: this is a REAL account and allow_live is false` | Sessão iniciada numa conta real. | Entre na conta demo. Só mude `allow_live` depois da secção 12. |
| `Configuration error: ...` | Erro no `config.json` (chave desconhecida, valor fora do intervalo, JSON inválido). | Leia a mensagem: diz qual a chave e o problema. Veja a secção 7. |
| `State file ... is unreadable or corrupt` | O `bot_state.json` foi editado ou danificado. | Abra o ficheiro e corrija-o, ou apague-o aceitando que o limite diário recomeça a partir do capital actual. |
| `order_check rejected the order: retcode=... comment=...` | A corretora recusaria a ordem. | Veja o código na tabela seguinte e o texto do comentário. |
| `no rate data` | Histórico do símbolo ainda não carregado. | Abra um gráfico desse símbolo e período no MT5 e aguarde. Verifique **Max bars in chart** (secção 5, passo 7). |
| `minimum volume 0.01 would risk ... > allowed ...; skipping` | Com o capital e o SL actuais, até o lote mínimo arriscaria mais do que o permitido. | Comportamento correcto (o bot nunca arredonda para cima). Aumente o capital da demo ou reduza `sl_atr_mult`, ou aceite menos operações. |
| Nunca abre operações | `dry_run` a `true`; sinais raros; fora do horário; spread acima do limite; volume abaixo do mínimo; limite diário atingido (`halted=True`); já existe posição do bot nesse símbolo. | Procure a razão no registo: `findstr /C:"skipping" /C:"signal" /C:"halt" bot.log`. Lembre-se de que depois de arrancar o bot espera pela vela seguinte. |
| O bot pára durante a noite | Suspensão ou hibernação do PC; reinício pelo Windows Update; MT5 fechado; kill switch presente; troca de conta (código 3). | Secções 11.4 e 11.5. Veja as últimas linhas do `bot.log` e o código de saída na janela. |
| `Terminal disconnected ...; reconnecting in 5s` | Perda de internet ou MT5 fechado. | O bot tenta de novo sozinho (5 s, 10 s, 20 s... até 120 s). Se persistir, verifique a internet e o MT5. |

Códigos de retorno (retcode) mais comuns:

| Código | Nome | Significado e o que fazer |
|---|---|---|
| 10004 | Requote | O preço mudou. O bot tenta de novo até 3 vezes com preço actualizado. |
| 10006 | Request rejected | A corretora recusou o pedido. Leia o comentário; tente mais tarde ou contacte o suporte. |
| 10014 | Invalid volume | Volume fora do mínimo/máximo/passo do símbolo. Veja **Specification** do símbolo. |
| 10015 | Invalid price | Preço inválido, muitas vezes por mercado muito rápido ou símbolo sem cotação. |
| 10016 | Invalid stops | SL ou TP demasiado perto do preço (`Stops level`). O bot já verifica; se acontecer, aumente `sl_atr_mult`/`tp_atr_mult`. |
| 10018 | Market is closed | Mercado fechado (fim de semana, feriado, sessão do símbolo). Ajuste `trading_hours_server`. |
| 10019 | No money | Margem insuficiente. Reduza o risco ou o número de posições, ou aumente o capital. |
| 10027 | AutoTrading disabled by client | Algo Trading desligado no terminal. Secção 5, passos 1 e 2. |
| 10030 | Unsupported filling mode | Modo de preenchimento não suportado. O bot escolhe IOC, FOK ou RETURN conforme o símbolo; se persistir, reporte o valor de `filling_mode` do símbolo à corretora. |

Códigos de saída do bot (aparecem na janela do `start_bot.bat`):

| Código | Significado |
|---|---|
| 0 | Saída normal (Ctrl+C ou kill switch com todas as posições fechadas). |
| 1 | Erro inesperado, ou kill switch sem conseguir fechar todas as posições (ficam protegidas por SL/TP). |
| 2 | Falha no arranque (configuração, ligação, Algo Trading, conta real recusada, símbolo inexistente). |
| 3 | Paragem de segurança em execução (o terminal passou para uma conta real não autorizada ou para outra conta). |

---

## 16. Executar os testes

Os testes substituem o pacote `MetaTrader5` por uma versão falsa, por isso **nunca** tocam no terminal nem na conta, e correm também em Linux ou macOS.

```
cd /d C:\mt5-bot
.venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
```

**Sucesso:** a última linha diz `133 passed in ...s`.

---

## 17. Pressupostos e limitações

**Pressupostos**

* O código foi desenvolvido e testado num ambiente **sem Windows e sem terminal MT5 real**, com um MT5 simulado nos testes. A lógica pura e o comportamento do bot estão testados, mas a interacção real com o terminal e com a corretora **não foi verificada**: valide em dry-run e depois em demo antes de confiar no bot.
* Os nomes de menus deste guia são os da interface do MT5 em inglês e do Windows em inglês ou português; podem variar ligeiramente com a versão.
* O `order_check` do MT5 devolve `retcode` 0 quando a ordem é válida (o bot aceita também 10009).
* As horas das velas e dos ticks do MT5 estão na hora do servidor da corretora. O bot usa a hora do último tick para decidir o horário de negociação e o "dia" do limite de perda diária.
* O dia do limite de perda diária é o **dia do servidor da corretora**, não o dia local nem UTC. Escolha deliberada: é o dia que a corretora usa para swaps e extractos diários e não depende do fuso horário do PC.
* As contas do tipo "contest" são tratadas como demo.
* Python 3.10 a 3.12, 64 bits. O pacote `MetaTrader5` publica também versões para 3.13 e 3.14, mas não são o alvo deste projecto. Os testes foram executados em Python 3.12.
* Requisitos mínimos: `MetaTrader5>=5.0.5430` (Novembro de 2025) e `numpy>=1.26`.

**Limitações**

* **Risco por operação:** o volume é calculado com `order_calc_profit` a partir do pior preço de entrada permitido por `deviation_points`. Mesmo assim, a perda real pode exceder o risco configurado por causa de: deslizamento no SL (o SL é executado a mercado), saltos de preço ao fim de semana ou em notícias, alteração da taxa de conversão para a moeda da conta e **comissões**, que o `order_calc_profit` não inclui (em contas com comissão por lote, some-a mentalmente ao risco).
* **Limite de perda diária:** usa o capital (equity) de **toda a conta**, incluindo operações manuais ou de outros robôs. A base é o capital na primeira vez que o bot vê esse dia do servidor; se arrancar a meio do dia, a base é o capital nesse momento. A perda mede-se face a essa base, não face ao máximo do dia.
* Com o mercado fechado (sem ticks novos), o "dia" não muda até chegar um tick novo.
* O horário só se aplica a entradas. As posições podem ficar abertas durante a noite e o fim de semana (swaps, spreads largos no rollover).
* Uma posição por símbolo por bot. Não corra duas instâncias com o mesmo `magic`; use um `magic` diferente para cada pasta/configuração.
* Em contas **netting** (uma posição por símbolo na conta), operações manuais no mesmo símbolo misturam-se com as do bot. Use contas **hedging** ou não negoceie manualmente esses símbolos.
* Depois de arrancar (ou reiniciar), o bot espera pela vela seguinte antes de agir; um sinal que ocorra durante a paragem perde-se.
* O arranque exige o Algo Trading ligado mesmo em dry-run, para validar a configuração final.
* Fechar a janela do bot à força não permite uma saída ordenada; prefira Ctrl+C ou o kill switch. As posições continuam protegidas por SL/TP.
* A estratégia de exemplo não tem vantagem comprovada e serve apenas para demonstração.
