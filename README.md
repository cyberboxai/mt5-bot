# Bot de negociação para MetaTrader 5, versão 3.1 (Python, Windows)

Guia de instalação e operação do bot `mt5_bot.py`, que negoceia automaticamente através do terminal MetaTrader 5 (MT5) instalado no seu PC Windows. Foi pensado para a corretora STARTRADER, mas funciona com qualquer corretora MT5. A versão 3.1 junta tudo num único sistema: braços com níveis de risco ganhos por evidência, exploração com micro risco desde o primeiro dia, adaptação ao tamanho da conta, protecção dos lucros, uma equipa de 22 agentes, um conselheiro de IA opcional e o painel **CYBERBOX Trading Office**.

**Documentação detalhada** (pasta `docs`):

| Documento | Para quê |
|---|---|
| [docs/DIAGNOSIS.md](docs/DIAGNOSIS.md) | Porque é que a 3.0 observava para sempre sem negociar, com evidência e o que foi corrigido. |
| [docs/UPGRADE.md](docs/UPGRADE.md) | Actualizar da 3.0 (ou anterior) e voltar atrás, passo a passo. |
| [docs/EVIDENCE_AND_RISK.md](docs/EVIDENCE_AND_RISK.md) | Braços, evidência, níveis T0 a T3, exploração, perfis de risco, piso de lucros, contas pequenas e grandes, gestão de posições, Doutor de Fluxo. |
| [docs/CONFIG.md](docs/CONFIG.md) | Referência completa do `config.json`. |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Cada código de bloqueio e o que fazer. |
| [docs/EXTENDING.md](docs/EXTENDING.md) | Acrescentar estratégias, políticas de gestão, agentes e elementos ao painel. |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | O que não foi verificado, pressupostos e limitações. |
| [docs/ux/UX_NOTES.md](docs/ux/UX_NOTES.md) | O painel: disposição, escritório, acessibilidade, capturas. |
| [panel/BRAND.md](panel/BRAND.md) | Identidade visual CYBERBOX no painel. |

## Índice

1. [O que é isto e o que mudou na 3.1](#1-o-que-é-isto-e-o-que-mudou-na-31)
2. [Aviso de risco (leia primeiro)](#2-aviso-de-risco-leia-primeiro)
3. [Requisitos](#3-requisitos)
4. [Instalar o Python](#4-instalar-o-python)
5. [Instalar o MT5 e entrar numa conta](#5-instalar-o-mt5-e-entrar-numa-conta)
6. [Preparar o MT5 para automação](#6-preparar-o-mt5-para-automação)
7. [Instalar o projecto](#7-instalar-o-projecto)
8. [Selecção de conta: demo ou real](#8-selecção-de-conta-demo-ou-real)
9. [Como o bot decide](#9-como-o-bot-decide)
10. [A equipa de agentes](#10-a-equipa-de-agentes)
11. [O conselheiro Claude (opcional)](#11-o-conselheiro-claude-opcional)
12. [O painel CYBERBOX Trading Office](#12-o-painel-cyberbox-trading-office)
13. [Configuração](#13-configuração)
14. [Primeira execução](#14-primeira-execução)
15. [Execução sem supervisão](#15-execução-sem-supervisão)
16. [Operações do dia a dia](#16-operações-do-dia-a-dia)
17. [Estender o bot](#17-estender-o-bot)
18. [Credenciais por variáveis de ambiente](#18-credenciais-por-variáveis-de-ambiente)
19. [Investigação, backtest, simulação e relatórios](#19-investigação-backtest-simulação-e-relatórios)
20. [Resolução de problemas](#20-resolução-de-problemas)
21. [Executar os testes](#21-executar-os-testes)
22. [Limitações e pressupostos](#22-limitações-e-pressupostos)

---

## 1. O que é isto e o que mudou na 3.1

O bot liga-se ao terminal MT5 aberto no seu PC (pacote oficial `MetaTrader5`), lê velas fechadas, decide sozinho quais combinações de estratégia podem negociar e com que risco, e envia ordens a mercado **sempre com stop loss (SL) e take profit (TP)** no servidor da corretora.

A 3.0 tinha um problema grave: na prática **observava para sempre e nunca negociava** (os portões de aceitação eram quase impossíveis de passar e não havia caminho intermédio). O diagnóstico completo, com evidência, está em [docs/DIAGNOSIS.md](docs/DIAGNOSIS.md).

| Área | 3.0 | 3.1 |
|---|---|---|
| Quem pode negociar | Um "campeão" por símbolo, depois de passar todos os portões, 30 trades em sombra e 7 dias de espera. Senão `NoTrade`. | **Braços** (estratégia + parâmetros + símbolo + período) com **níveis**: T0 sombra, T1 probação (micro risco), T2 activo, T3 comprovado. A evidência (sombra + real + investigação descontada) decide, braço a braço. |
| Primeiros trades | Dias ou semanas, ou nunca. | Minutos: trades de **exploração** com micro risco, pagos por um orçamento diário e semanal limitado (0,2%/0,5% em demo, 0,1%/0,3% em real no perfil equilibrado). |
| Risco | Valores fixos por conta. | **Perfis** (conservador, equilibrado, agressivo) por baixo de tectos rígidos; risco por nível, Kelly fraccionado, anti-martingale, risco aberto total. |
| Lucros | Disjuntor de drawdown. | Também um **piso de lucros que só sobe** (parte de cada novo máximo fica protegida) e risco proporcional à almofada acima do piso. |
| Tamanho da conta | Contas pequenas sem evidência (o lote mínimo bloqueava a sombra). | De 50 a 1 000 000: mapa de viabilidade, excepção consciente do lote mínimo, divisão de ordens grandes, depósitos e levantamentos excluídos do desempenho. |
| Mercados | 1 símbolo, 1 período, 7 estratégias. | Universo de até 6 símbolos viáveis em M5, M15 e H1, 12 estratégias, alocação consciente do regime de mercado. |
| Porque não negoceia? | Uma linha de estado vaga. | **Funil de decisão**: cada barra, sinal e bloqueio com um código. `--diagnose` e o separador **Porque não estou a negociar?** dizem o motivo em português e quanto vale cada filtro. |
| Ajuste automático | Nenhum. | **Doutor de Fluxo**: relaxa um passo de cada vez, dentro de limites fixos, só filtros que não protegem, e reverte se piorar. Nunca mexe no risco. |
| Gestão das posições | SL/TP fixos. | Políticas por braço (fixo, break-even, stop móvel, saída por tempo) escolhidas por evidência emparelhada; protecção em picos de spread e antes de notícias. Só apertam. |
| Sistema | Núcleo, equipa e painel com bases de dados separadas e modos. | **Um sistema**: um ponto de entrada, uma base de dados por conta (`mt5bot.sqlite`), migração automática com cópia de segurança e `--rollback-upgrade`. |
| Painel | Escritório e dez painéis. | Redesenhado com a identidade CYBERBOX: barra de topo com o essencial, equipa à esquerda, escritório com personagens animadas e caminhos A*, câmara, minimapa, seguir agente, repetição, paleta Ctrl+K, preferências de acessibilidade, telemóvel. |

**O que não mudou e não pode mudar por configuração:** todas as ordens levam SL e TP; os tectos de `mt5bot/limits.py` (2% por trade, 5% por dia, 4% de risco aberto, 20% de drawdown, 5 posições), o disjuntor de drawdown, o limite diário, o kill switch, o Guardião e a camada de risco aplicam-se sempre. **Nenhum agente nem o conselheiro pode** aumentar o tamanho, transformar um "não" num "sim", enviar ou alterar ordens, nem alargar ou remover um SL. Não há código gerado em tempo de execução. Dinheiro real continua a exigir uma confirmação explícita, uma única vez.

### 1.1 Estrutura do projecto

```
mt5_bot.py              ponto de entrada (python mt5_bot.py ...)
supervisor.py           supervisor que mantém o bot (e o painel) a correr
config.json             configuração (config_version "3.1")
mt5bot\                 código do bot: bot.py, risk.py, limits.py, arms.py, evidence.py, tiers.py,
                        capital.py, profiles.py, allocation.py, funnel.py, flow.py, management.py,
                        regime.py, research.py, opportunity.py, upgrade.py, db.py, ...
mt5bot\strategies\      biblioteca de estratégias (um ficheiro por estratégia)
mt5bot\team\            equipa de agentes: protocolo, agregação, aprendizagem, ablação
mt5bot\team\agents\     os 22 agentes, por departamento
mt5bot\advisor\         conselheiro Claude opcional
mt5bot\sim\             mercado sintético e terminal simulado (--simulate)
panel\                  painel web (servidor só com a biblioteca padrão do Python)
panel\static\           página, estilos, tipos de letra, logótipo e scripts (tudo local, sem CDN)
docs\                   documentação detalhada
tests\                  testes automáticos (pytest) e tests\js (Node.js, motor do escritório)
start_bot.bat           arranca só o bot
start_supervisor.bat    arranca o supervisor (recomendado; arranca também o painel)
start_panel.bat         arranca só o painel (ou a demonstração, com --demo)
install_autostart.bat   regista o arranque automático no Agendador de Tarefas
uninstall_autostart.bat remove esse registo
```

Dentro de cada pasta de conta (`state\<servidor>_<login>\`) fica **`mt5bot.sqlite`**: diário de operações (reais e em sombra), braços e a sua evidência, investigação, funil de decisão, ajustes do Doutor de Fluxo, fluxos de caixa, eventos e dossiers da equipa, chamadas ao conselheiro e auditoria.

---

## 2. Aviso de risco (leia primeiro)

> * **Nenhum software garante lucro.** Este bot também não. Pode perder dinheiro, incluindo todo o capital da conta.
> * **A maioria dos investidores particulares que negoceiam CFD e forex perde dinheiro.** As próprias corretoras publicam percentagens de contas perdedoras, normalmente entre 70% e 80%.
> * **A 3.1 negoceia mais cedo do que a 3.0, de propósito, com micro risco.** Trades de exploração sem vantagem perdem os custos; o orçamento de exploração limita essa perda (por omissão 0,2% por dia em demo e 0,1% em real), não a elimina. Com estratégias sem vantagem real, o resultado esperado é uma pequena perda, não um ganho.
> * **Sistemas adaptativos podem enganar-se com o acaso.** A evidência é partilhada e encolhida para uma média céptica, as subidas de nível exigem muitos trades e as descidas são imediatas, mas nenhuma técnica elimina este risco.
> * Resultados em sombra e na investigação são estimativas. A execução real tem deslizamento, spreads variáveis, saltos de preço e falhas de internet, energia ou da corretora.
> * **Mais complexidade não é mais vantagem.** A equipa de agentes e o conselheiro de IA não criam vantagem: só reduzem ou vetam, e também podem recusar operações que teriam dado lucro.
> * **O conselheiro Claude custa dinheiro**, cobrado pela Anthropic a si. Defina sempre também um limite de gasto na consola da Anthropic.
> * **A simulação e o modo de demonstração usam um mercado sintético.** Lucros, níveis ou vitórias que lá apareçam não são evidência sobre mercados reais.
> * Comece numa conta **demo** durante várias semanas. Passe a real só com capital pequeno que pode perder na totalidade, de preferência com o perfil `conservative`. Antes, veja `python mt5_bot.py --risk-report`. Este software é fornecido sem qualquer garantia.

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
3. **Tools > Options > Charts > Max bars in chart**: escolha **Unlimited** (ou pelo menos 100000). A investigação pede até 12 meses de velas por período; com um valor baixo recebe menos histórico (e o `--diagnose` diz que a investigação falhou por falta de velas).
4. **View > Market Watch** (Ctrl+M), botão direito, **Show All**. Anote o nome exacto do símbolo, por exemplo `EURUSD` ou `EURUSD.m`. O bot detecta sufixos sozinho (secção 20), mas confirmar não custa.
5. O cabeçalho do Market Watch mostra a **hora do servidor**. O bot detecta automaticamente a diferença para UTC a partir dos ticks; `trading_hours_server` usa a hora do servidor.

---

## 7. Instalar o projecto

1. Crie a pasta e entre nela:
   ```
   mkdir C:\mt5-bot
   cd /d C:\mt5-bot
   ```
2. Copie para `C:\mt5-bot` todos os ficheiros e pastas do projecto (`mt5_bot.py`, `supervisor.py`, `config.json`, as pastas `mt5bot`, `panel`, `docs` e `tests`, os ficheiros `.bat` e `.ps1`, `requirements.txt`, `requirements-dev.txt`, `README.md`).
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

O perfil de risco (secção 13) tem números próprios para contas reais: no equilibrado, os trades de exploração e de probação (T1) arriscam 0,05% em real contra 0,10% em demo, e o orçamento de exploração é 0,10% por dia e 0,30% por semana em real contra 0,20% e 0,50% em demo. Os tectos rígidos do código são iguais para todos: 2,0% por operação, 5,0% de perda diária, 4,0% de risco aberto, 20% de drawdown. Tabela completa em [docs/EVIDENCE_AND_RISK.md](docs/EVIDENCE_AND_RISK.md#5-perfis-de-risco).

### 8.4 Estado separado por conta

Cada conta tem a sua pasta `state\<servidor>_<login>\`, por exemplo `state\STARTRADER-Demo_12345678\`. Lá ficam:

* `mt5bot.sqlite`: diário de operações (reais e em sombra), braços e evidência, investigação, funil de decisão, capital, fluxos de caixa, equipa e auditoria;
* `risk_state.json`: capital do início do dia e pico de capital;
* `RESUME_REQUIRED.txt`: só existe se o disjuntor de drawdown disparou;
* `optimizer.log`: registo da investigação em segundo plano.

O histórico da demo **nunca** influencia as decisões da conta real e vice-versa. Os relatórios também ficam separados em `reports\<servidor>_<login>\`.

### 8.5 Contas hedging e netting

O bot detecta o tipo de conta (`margin_mode`):

* **hedging:** pode haver várias posições no mesmo símbolo; o bot fecha a sua posição pelo número (ticket);
* **netting:** há uma única posição por símbolo; o bot fecha com uma ordem oposta do mesmo volume, altera SL/TP com `TRADE_ACTION_SLTP` e **não abre** se já existir uma posição de outra origem (manual ou outro robô) nesse símbolo.

---

## 9. Como o bot decide

Em linguagem simples: o bot acompanha centenas de **braços** (cada estratégia, com uns
parâmetros, num símbolo e num período) a negociar em papel. Cada braço tem uma ficha de
evidência. Os braços sem evidência suficiente podem fazer, de vez em quando, um trade real
minúsculo pago por um orçamento de exploração. Quando a evidência de um braço fica forte, ele
sobe de nível e arrisca mais; quando enfraquece, desce logo. Os detalhes e todos os números
estão em [docs/EVIDENCE_AND_RISK.md](docs/EVIDENCE_AND_RISK.md).

```
 Biblioteca: 12 estratégias com parâmetros limitados (código revisto)
   ema_cross, donchian, rsi_reversion, bollinger, session_breakout, ny_breakout,
   momentum_pullback, vwap_reversion, range_fade, vol_contraction, e versões filtradas
            |
            v
 Braços = estratégia x parâmetros x símbolo (universo até 6) x período (M5, M15, H1)
   cada um com livro em SOMBRA (trades em papel, nunca enviados)
            |
   Investigação walk-forward em segundo plano -> expectativa inicial DESCONTADA (peso 0,3)
            |
            v
 Evidência por braço: sombra + real + investigação, partilha com a família,
   média céptica -0,03R, meia-vida 120 dias  ->  P(E>0) e intervalo de 90%
            |
            v
 Níveis:  T0 sombra | T1 probação (micro risco) | T2 activo | T3 comprovado
   sobe um nível de cada vez (no máximo de 4 em 4 horas); desce imediatamente
            |
            v
 Em cada barra fechada: sinais dos braços -> ALOCAÇÃO (uma entrada por símbolo)
   ordenados por nível e P(E>0) no regime actual
   braço T0: sorteio de Thompson + vaga de exploração + orçamento -> trade T1
            |
            v
 Dimensionamento: o MENOR de tecto, perfil, nível, Kelly, orçamento, piso de lucros,
   risco aberto, anti-martingale, correlação, liquidez
            |
            v
 Equipa de 22 agentes (+ conselheiro opcional): só reduzem ou vetam
            |
            v
 Camada de risco (veto final) -> ordem com SL e TP -> política de gestão do braço
            |
 Cada passo deixa um código no FUNIL (porquê não negociou, quanto valeu cada filtro)
```

Pontos importantes:

* **Camada de risco:** as estratégias só propõem uma direcção e uma volatilidade (ATR). O módulo `mt5bot/risk.py` decide se a operação pode abrir e com que volume. O mesmo código serve a sombra, a investigação e o real.
* **Comportamentos proibidos (no código e nos testes):** martingale, aumentar o volume depois de uma perda, juntar posições em perda, grelhas, ordens sem SL e TP, remover ou alargar um SL, negociar com spread ou deslizamento acima do limite, negociar em janelas de notícias (se o filtro estiver activo).
* **Nada de código gerado em tempo real:** o bot só escolhe entre estratégias e valores de parâmetros definidos no código. Não usa `eval`, `exec` nem descarrega código.
* **Uma posição aberta fica com o braço que a abriu:** as suas regras de saída e a sua política de gestão, mesmo que outro braço suba de nível.
* **Custos realistas na sombra e na investigação:** entrada na abertura da vela seguinte, spread de cada vela, deslizamento, comissão por lote, swap negativo por noite e, quando o SL e o TP são tocados na mesma vela, assume-se que o SL foi atingido primeiro.
* **Doutor de Fluxo:** se o bot estiver parado muitas horas com sinais e mercado aberto, relaxa um passo (horário, tolerância de spread, limiar de T1, exigência da equipa, universo) dentro de limites fixos, revê depois de 10 trades ou 48 horas e reverte se piorar. Nunca mexe no risco.

---

## 10. A equipa de agentes

Em linguagem simples: sempre que um braço quer abrir uma operação, a proposta passa por uma reunião. Especialistas verificam os dados, o regime de mercado, a sessão, as notícias e a exposição; os analistas de estratégia votam a favor ou contra; a Directora de Investimentos agrega os votos com pesos aprendidos; a Gestora de Risco tem a última palavra. O resultado é um **multiplicador entre 0 e 1** que só pode **reduzir** o volume que a camada de risco calculou. A equipa não cria operações: sem proposta de um braço não há reunião.

Cada agente é uma classe Python no código (`mt5bot\team\agents\`), corre com tempo limite e isolado dos outros. Se um agente falhar, a reunião continua sem ele (e os agentes de segurança falham no sentido seguro: sem a Escriturária de Dados, por exemplo, não há entradas).

### 10.1 Os 22 agentes

| Nome | Função | Departamento | Pode decidir | Não pode | id |
|---|---|---|---|---|---|
| Dina | Escriturária de Dados | Inteligência de Mercado | pausar entradas quando os dados estão maus | abrir ou fechar posições | `data_clerk` |
| Rui | Detector de Regime | Inteligência de Mercado | classificar o regime de mercado | votar ou dimensionar trades | `regime_detector` |
| Sara | Analista de Sessões e Liquidez | Inteligência de Mercado | reduzir o tamanho em horas pouco líquidas | aumentar o tamanho | `session_analyst` |
| Nuno | Sentinela de Notícias e Calendário | Inteligência de Mercado | bloquear entradas perto de eventos de alto impacto | abrir trades ou ler instruções de notícias | `news_sentinel` |
| Carla | Analista de Correlação e Exposição | Inteligência de Mercado | reduzir ou bloquear risco duplicado entre moedas | aumentar a exposição | `correlation_analyst` |
| Tomás | Analista de Tendência | Mesa de Estratégia | votar a favor ou contra (e apresentar a proposta dos braços da sua família) | dimensionar acima da base | `trend_analyst` |
| Mara | Analista de Reversão à Média | Mesa de Estratégia | votar a favor ou contra (e apresentar a proposta dos braços da sua família) | dimensionar acima da base | `mean_reversion_analyst` |
| Bruno | Analista de Rupturas e Volatilidade | Mesa de Estratégia | votar a favor ou contra (e apresentar a proposta dos braços da sua família) | dimensionar acima da base | `breakout_analyst` |
| Mateus | Analista Multi-Período | Mesa de Estratégia | votar com o enviesamento do período superior | propor trades | `mtf_analyst` |
| Lia | Meta-Etiquetadora | Mesa de Estratégia | votar com a probabilidade validada fora da amostra (ou abster-se) | propor trades | `meta_labeler` |
| Rita | Gestora de Risco | Risco e Controlo | vetar qualquer entrada (veto final) | ser vencida por votos, pesos ou pelo conselheiro | `risk_manager` |
| Gil | Guardião | Risco e Controlo | congelar entradas ou accionar o kill switch | abrir trades ou relaxar limites | `guardian` |
| Edu | Operador de Execução | Execução | enviar a ordem aprovada com SL/TP e verificar spread e preço | alterar o tamanho aprovado | `execution_trader` |
| Paula | Gestora de Posições | Execução | apertar stops, fechos parciais e de tempo (só reduzem risco) | alargar ou remover um SL | `position_manager` |
| Quim | Investigador Quantitativo | Investigação e Revisão | correr a investigação walk-forward e os livros em sombra | mudar níveis sem evidência | `quant_researcher` |
| Vera | Auditora de Validação | Investigação e Revisão | sinalizar sobreajuste e fugas de dados | promover braços | `validation_auditor` |
| Pedro | Analista de Desempenho | Investigação e Revisão | actualizar pesos e calibração, pôr agentes no banco | mexer no risco | `performance_analyst` |
| Rosa | Repórter | Investigação e Revisão | escrever relatórios e notificações | decidir trades | `reporter` |
| Olavo | Engenheiro de Operações | Operações e Liderança | vigiar o terminal, a máquina, o painel e o conselheiro | decidir trades | `ops_engineer` |
| Ciro | Estratega Claude (ligação ao conselheiro) | Operações e Liderança | pedir ao conselheiro para reduzir ou vetar | aumentar o tamanho, criar trades ou mexer em ordens | `claude_strategist` |
| Flávio | Doutor de Fluxo (governador de actividade) | Operações e Liderança | explicar em linguagem simples porque o bot não negoceia e propor ajustes limitados de horário, spread, limiares e universo | mexer no risco, nos limites, no piso ou nos SL/TP | `flow_doctor` |
| Isabel | Directora de Investimentos (Orquestradora) | Operações e Liderança | conduzir o protocolo e submeter o pedido de trade | ultrapassar o Risco ou o Guardião | `cio` |

* **Nunca vão para o banco:** `data_clerk`, `risk_manager`, `guardian`, `ops_engineer` e `flow_doctor`.
* **Não podem ser desligados na configuração:** `data_clerk`, `risk_manager`, `guardian`, `execution_trader`, `ops_engineer` e `cio`.
* Os restantes desligam-se em `team.agents`, por exemplo `"team": {"agents": {"news_sentinel": false}}`.
* Os três analistas de estratégia são donos de **famílias**: Tomás (tendência: `ema_cross`, `momentum_pullback`), Bruno (rupturas: `donchian`, `session_breakout`, `ny_breakout`, `vol_contraction`, `bollinger` em modo squeeze), Mara (reversão: `rsi_reversion`, `vwap_reversion`, `range_fade`, `bollinger`). O dono apresenta a proposta do braço; os outros votam com a sua leitura independente do mercado.

### 10.2 O protocolo de decisão

```
 O braço escolhido pela alocação propõe BUY ou SELL   <- a ÚNICA origem de uma ordem
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
 5. Camada de risco (Rita = mt5bot/risk.py): veto final
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
 Dossier guardado em mt5bot.sqlite; o resultado é medido quando a operação fecha
```

**Regime:** cada braço tem evidência própria por regime de mercado; com pelo menos `regime_min_trades` (20) trades num regime e P(E>0) abaixo de 0,25 nesse regime, o braço fica de fora (secção 10 de [docs/EVIDENCE_AND_RISK.md](docs/EVIDENCE_AND_RISK.md#10-regimes-de-mercado)). A Directora aplica ainda a regra antiga ao nível da estratégia: com 20 resultados num regime e limite superior a 95% da expectativa negativo, não a consulta nesse regime.

O diagrama descreve o modo `full`. Em `baseline` (e em `auto` antes de a equipa provar valor) só os bloqueios de segurança da secção 10.5 e a camada de risco podem impedir uma entrada; as reduções e vetos da equipa, da Directora e do conselheiro ficam registados como contrafactuais.

O multiplicador final nunca passa de 1 e é aplicado **depois** de a camada de risco calcular o volume. Se o volume reduzido ficar abaixo do lote mínimo sem cumprir o risco, a entrada é recusada (nunca é arredondada para cima).

### 10.3 Os poderes da Gestora de Risco e do Guardião

* **Rita (Gestora de Risco)** é a camada de risco (`RiskManager.approve_entry`). O veto dela é final: nenhum voto, peso, conselheiro ou modo o ultrapassa, e ela nunca vai para o banco. Faz uma pré-verificação barata no início (para não gastar uma chamada ao conselheiro numa entrada já bloqueada) e a verificação definitiva no momento do envio.
* **Gil (Guardião)** é um caminho de código **independente**: o bot chama-o directamente no seu ciclo, fora da reunião, por isso continua a vigiar mesmo que a equipa falhe. Verifica invariantes a cada ciclo:
  * posições do bot sem SL/TP depois da reparação automática;
  * mais posições do que os tectos rígidos;
  * posições na corretora que não estão no diário;
  * risco aberto acima do limite (com 10% de tolerância) ou risco por operação acima do tecto;
  * ordens enviadas enquanto a negociação estava parada.

  Qualquer violação **congela novas entradas** em todos os modos (as posições abertas continuam protegidas). O congelamento levanta-se sozinho depois de 3 verificações limpas seguidas (`team.guardian_freeze_clear_checks`). Três situações graves pedem o **kill switch** (cria o ficheiro `KILL`, fecha as posições do bot e pára): uma posição sem SL/TP durante 3 verificações, posições acima dos tectos, ou ordens enviadas com a negociação parada. Pode desligar este último passo com `"guardian_kill_enabled": false` (fica só o congelamento).

### 10.4 Aprendizagem: pesos, calibração, banco e ablação

Tudo é aprendido devagar, com limites, e só com operações **já fechadas** (nunca com o futuro):

* **Resultado de cada proposta:** quando uma proposta fica resolvida, o bot sabe se a direcção estava certa, mesmo que a equipa a tenha recusado, porque cada braço negoceia também em sombra (o mesmo sinal, na vela seguinte, com as regras do backtest).
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
| `auto` (omissão) | Usa o núcleo base (só os bloqueios de segurança e a camada de risco) **até** a ablação mostrar, com pelo menos `ablation_min_samples` (50) decisões resolvidas e 90% de confiança, que a equipa completa supera o núcleo base fora da amostra. Depois passa a `full`, e volta a `baseline` se deixar de superar. | Sempre, salvo razão concreta. |
| `full` | As reduções e vetos da equipa e do conselheiro aplicam-se já. | Para observar a equipa em demo antes de haver evidência. |
| `baseline` | Multiplicador 1: a equipa não reduz o tamanho. A equipa e o conselheiro continuam a opinar e ficam registados como contrafactuais, para medir o seu valor. | Para comparar. |

**Em todos os modos** aplicam-se os bloqueios de segurança: dados maus, alerta de fuga de dados da Auditora, congelamento do Guardião, pausa pelo painel e camada de risco. Em `baseline` (e em `auto` antes de haver prova) **o conselheiro não altera o volume**: o veredicto fica registado e é medido, mas só conta em `full`. É uma escolha deliberada: a equipa só muda decisões depois de provar valor.

Pode forçar o modo numa execução com `--team-mode full`, `--team-mode baseline` ou `--team-mode auto` (sobrepõe-se ao `config.json`). A equipa não se desliga por completo: para que só registe opiniões, use `baseline`.

### 10.6 O dossier de decisão

Cada reunião produz um dossier em `mt5bot.sqlite`, visível no painel (separador **Registo de decisões**, ou clique numa reunião no escritório):

* proposta (braço, nível, símbolo, lado, estratégia, parâmetros, P(E>0), risco), regime e sessão;
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
| Investigação semanal | `claude-opus-5-5` | Sugerir parâmetros **da grelha já existente** para a próxima investigação; entram como braços em sombra, sem atalhos. |
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
* Cada chamada fica registada em `mt5bot.sqlite` (finalidade, modelo, resultado, tokens, custo estimado, latência, hash e versão ocultada do prompt, resposta). A chave nunca é guardada.

---

## 12. O painel CYBERBOX Trading Office

O painel é uma página web com a identidade CYBERBOX que mostra, ao vivo, o que o bot está a fazer: a barra de topo com o essencial da conta, a equipa, um escritório animado onde cada personagem é um agente real, o separador **Porque não estou a negociar?** e os painéis com números. Corre num **processo separado** do bot, abre as bases de dados **só para leitura** e, por omissão, **não tem nenhum botão que mexa na negociação**. Se o painel falhar, travar ou for fechado, o bot não é afectado (há um teste automático que mata o painel a meio e confirma que o bot continua).

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
O painel arranca uma simulação do bot verdadeiro sobre um mercado sintético (EURUSD, GBPUSD, USDJPY e outros símbolos do universo), com braços a explorar desde os primeiros minutos, a equipa em `full` e um conselheiro simulado que passa de propósito por falhas (degradado, sem crédito) para ver como o bot reage. Abra **http://127.0.0.1:8765/**. Uma faixa roxa diz **SIMULAÇÃO** em todas as páginas.

* Os dados ficam em `state_demo\` (pode apagar a pasta quando quiser).
* A janela mostra um token de uso único para experimentar as acções de controlo: `DEMO: control actions use this one-time token: ...`.
* `--demo-speed 4` (velas por segundo) e `--demo-fast-forward 300` (velas iniciais à velocidade máxima) controlam o ritmo.
* **Nada do que aparece na simulação é evidência sobre mercados reais.**

A mesma simulação, sem painel, corre com `python mt5_bot.py --simulate` (secção 19).

### 12.3 O ecrã

* **Barra de topo** (sempre visível): o selo da conta (**DEMO**, **REAL** a vermelho ou **SIMULAÇÃO**), o capital, o P&L de hoje, a **almofada** acima do piso de lucros, a barra do **orçamento de exploração** de hoje, o modo (Exploração, Probação, Activo, Em pausa, Disjuntor), a lâmpada do conselheiro, a hora do servidor e as sessões de Tóquio, Londres e Nova Iorque (acesas quando abertas).
* **Equipa** (à esquerda): os 22 agentes por departamento, com retrato, função e estado. Clicar abre a ficha e segue a personagem no escritório; ◎ só segue. No telemóvel abre com ☰.
* **Linha de estado:** o que o bot está a fazer e o bloqueio principal das últimas 6 horas, em português.
* **Barra de baixo:** os últimos eventos e a **repetição das últimas 24 horas** (barra, Reproduzir, velocidade, Ao vivo).
* **Ctrl+K** (ou "/"): paleta de comandos para ir a qualquer separador, seguir ou abrir um agente, ver uma sala, mudar preferências, repetir o dia ou abrir a visita guiada.
* **Preferências** (⚙): tema, alto contraste, movimento reduzido, nível de efeitos e monitor de desempenho. **?** abre a visita guiada.

### 12.4 O escritório

Dez salas ligadas por dois corredores: Sala de Negociação, Laboratório de Investigação, Gabinete de Risco, Cabine do Conselheiro, Redacção, Sala do Conselho, Mesa de Execução, Sala de Operações, Canto da Análise e Sala de Descanso (o banco).

* **Cada personagem é um agente** e só se mexe quando o bot faz algo: senta-se à secretária a escrever (a analisar) ou a ler (a rever), fala (o balão mostra o que disse), vai à Sala do Conselho numa reunião, mostra um bilhete amarelo numa ordem, um sinal de stop num veto, celebra uma vitória, vai para o sofá quando está no banco, mostra "!" num erro. O Ciro fica transparente quando o conselheiro não está disponível. As personagens andam pelos corredores com caminhos calculados (A*).
* **Câmara:** arraste para mover, roda ou pinça para aproximar, duplo clique para focar, setas e +/- no teclado, 0 para ver tudo, "Ir para a sala..." e minimapa clicável.
* **Ecrãs:** cotações a deslizar, próxima notícia, P&L e capital, risco aberto no cofre, reunião em curso, servidores, lâmpada do conselheiro. À noite (sem sessões abertas) o escritório escurece.
* **Reunião de decisão:** um cartão mostra os votos, o conselheiro, o risco e o resultado; "Abrir dossier" mostra a decisão completa.
* **Fluxo de eventos** à direita, com filtros por agente, símbolo e gravidade.

O escritório respeita "reduzir movimento" (do sistema ou das preferências) e pára de desenhar quando o separador do navegador não está visível. Detalhes e capturas: [docs/ux/UX_NOTES.md](docs/ux/UX_NOTES.md).

### 12.5 Os separadores

| Separador | O que mostra |
|---|---|
| **Porque não estou a negociar?** | Uma frase com o maior travão, o funil (barras, sinais, bloqueios, ordens), os bloqueios principais com o valor de cada filtro (protege ou custa), o Doutor de Fluxo, o orçamento de exploração, os braços por nível, a actividade por hora e o que a conta consegue pagar. |
| **Visão geral** | Capital, P&L de hoje, 7 e 30 dias e total, posições, modo da equipa, conselheiro, curva de capital, indicadores de risco, braços por nível e posições abertas. |
| **Desempenho** | Métricas das operações reais e em sombra, distribuição dos R, retornos mensais, R acumulado, expectativa móvel e comparações. |
| **Trades** | O diário (reais ou sombra) com filtros, CSV, MAE e MFE em R, deslizamento, regime e multiplicador da equipa. Clique para o dossier. |
| **Equipa** | Classificação dos agentes, pesos ao longo do tempo, banco e departamentos. |
| **Estratégias e Investigação** | Braços por nível com P(E>0) e intervalo, trades em sombra e reais, política de gestão; mudanças de nível e de política com o motivo; investigação walk-forward e os seus alertas; hipóteses do conselheiro. |
| **Risco e Exposição** | VaR histórico (estimativa), pior cenário do risco aberto, exposição por símbolo e moeda, correlação, cada limite com o valor efectivo, o tecto e o uso, e os disjuntores. |
| **Mercados** | Sessões, regime por símbolo, volatilidade, spread por hora, notícias, oportunidades e viabilidade. |
| **Conselheiro Claude** | Estado, chamadas e custo de hoje, latência, valor acrescentado, vereditos e erros. |
| **Saúde do sistema** | Bot a correr, ciclos, reinícios, métricas de 24 horas, acções de controlo, alertas, auditoria e o fim do `bot.log`. |
| **Registo de decisões** | Todas as reuniões, com pesquisa. Clique para o dossier. |

Todos os gráficos têm **Tabela** e **CSV**. Com poucos dados um gráfico diz "Dados insuficientes" em vez de mostrar números enganadores.

**Como são calculadas as métricas:** R é o resultado dividido pelo risco inicial (distância ao SL); expectativa é o R médio; factor de lucro é a soma dos ganhos a dividir pela soma das perdas; Sharpe, Sortino e Calmar usam variações diárias do capital, anualizadas com 252 dias (pelo menos 10 dias); VaR histórico a 1 dia (95%) precisa de 20 dias e é uma estimativa do passado; MAE e MFE são o pior e o melhor ponto do trade em R.

### 12.6 Acções de controlo (desligadas por omissão)

O painel pode ter três botões no separador **Saúde do sistema**: **Pausar novas entradas**, **Retomar entradas** e **Kill switch**. Só funcionam se **as duas** condições se verificarem:

1. `"panel": {"control_actions_enabled": true}` no `config.json`;
2. um token definido no ambiente (nunca em ficheiros):
   ```
   setx MT5BOT_PANEL_TOKEN "uma-frase-longa-e-aleatoria-com-mais-de-20-caracteres"
   ```
   Feche e abra um **novo** `cmd` e reinicie o supervisor (ou o painel).

Cada acção pede o token (escrito na altura, nunca guardado no navegador) e uma confirmação, é verificada contra pedidos de outros sites (CSRF e origem), está limitada a 5 por minuto e fica registada na auditoria (separador **Saúde do sistema** e `mt5bot.sqlite`), com sucesso ou recusa. O que fazem:

| Acção | Efeito | As posições abertas |
|---|---|---|
| Pausar novas entradas | Cria `PAUSE_ENTRIES` na pasta da conta. O bot deixa de abrir operações. | Continuam com SL/TP e gestão normal. |
| Retomar entradas | Apaga `PAUSE_ENTRIES`. Não desliga disjuntores, limites nem o Guardião. | Sem alteração. |
| Kill switch | Cria o ficheiro `KILL`. | O bot fecha **as suas** posições e pára (secção 16). |

O painel nunca pode relaxar limites, aumentar o risco, enviar ordens ou mudar a configuração.

### 12.7 Acesso a partir de outro computador

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

## 13. Configuração

A referência completa de todas as chaves está em [docs/CONFIG.md](docs/CONFIG.md). O essencial:

| Chave | Omissão | Para quê |
|---|---|---|
| `risk_profile` | `"balanced"` | `conservative`, `balanced` ou `aggressive` (tabela em [docs/EVIDENCE_AND_RISK.md](docs/EVIDENCE_AND_RISK.md#5-perfis-de-risco)). Em conta real comece por `conservative`. |
| `symbols` | `["EURUSD"]` | Símbolos sempre incluídos. |
| `universe.candidates` / `universe.max_symbols` | 9 pares e ouro / 6 | De onde o bot escolhe os símbolos viáveis para o capital da conta. |
| `universe.timeframes` | `["M5", "M15", "H1"]` | Períodos dos braços. |
| `trading_hours_server` | `[2, 22]` | Janela de entradas na hora do **servidor**. |
| `tiers.exploration_enabled` / `tiers.exploration_slots` | `true` / 2 | Trades de exploração com micro risco. |
| `flow_doctor.enabled` | `true` | Ajustes automáticos limitados quando o bot está parado sem razão. |
| `account_mode` / `real_money_confirmed` | `"any"` / `false` | Secção 8. |
| `dry_run` | `false` | Tudo menos enviar ordens. |
| `team.team_mode` | `"auto"` | Secção 10.5. |
| `claude.enabled` | `false` | Secção 11. |
| `panel.enabled` / `panel.port` | `true` / 8765 | Secção 12. |

Os limites antigos (`risk_per_trade_pct_demo`, `max_daily_loss_pct_real`, ...) continuam aceites, mas só podem **apertar** o perfil. O bot relê o ficheiro a cada minuto; alargar um limite, escolher um perfil mais agressivo ou mudar símbolos e o universo exige reiniciar.

Um `config.json` de uma versão anterior é migrado com avisos: as chaves que deixaram de ter efeito vão para `_legacy_v30` (nada é descartado em silêncio) e o resultado fica em `config.migrated.json` para rever ([docs/UPGRADE.md](docs/UPGRADE.md)).

---

## 14. Primeira execução

### 14.1 Em demo, primeiro em seco

1. Com o MT5 aberto numa conta **demo** e o ambiente virtual activo:
   ```
   cd /d C:\mt5-bot
   .venv\Scripts\activate
   python mt5_bot.py --dry-run
   ```
   **Sucesso:** aparece a faixa com `DEMO`, o universo escolhido (`Universe: EURUSD, ... on M5, M15, H1`), os braços criados em sombra e, ao fim de alguns minutos, linhas de sinais e de exploração. Em seco nada é enviado (`DRY RUN: would send ...`).
2. Noutra janela:
   ```
   python mt5_bot.py --status
   python mt5_bot.py --diagnose --hours 1
   ```
   **Sucesso:** o estado mostra braços por nível, o orçamento de exploração e o piso; o diagnóstico mostra barras avaliadas e sinais.
3. Abra o painel (secção 12) e veja o separador **Porque não estou a negociar?**.
4. Pare com Ctrl+C e arranque a sério em demo: `start_supervisor.bat`.

### 14.2 O que esperar

* **Nos primeiros minutos:** os braços começam em sombra (T0). Em cada barra fechada há sinais; alguns braços fazem trades de exploração com micro risco (T1), no máximo 2 ao mesmo tempo, dentro do orçamento do dia.
* **Em segundo plano:** a investigação de arranque (rápida, 6 meses de histórico) dá a cada braço uma expectativa inicial descontada; a investigação completa corre ao fim de semana.
* **Dias a semanas:** braços com evidência positiva passam a T1 por mérito; a T2 só com pelo menos 50 trades e P(E>0) >= 0,80; a T3 com 100 trades e 0,93. Muitos braços nunca sobem: é o comportamento pretendido.
* **Dias sem trades** acontecem (mercado parado, orçamento gasto, braços sem vantagem). O `--diagnose` diz porquê.

### 14.3 Caminho recomendado

1. Demo, perfil `balanced`, várias semanas. Acompanhe os níveis e o funil.
2. Veja `python mt5_bot.py --risk-report --risk-profile conservative` e decida o perfil com os números à frente.
3. Real com capital pequeno, perfil `conservative`, `real_money_confirmed` e o supervisor.

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

O `health.json` (na pasta do projecto) é reescrito a cada minuto com: hora do último ciclo e do último tick, capital, posições abertas, braços por nível, orçamento de exploração, piso de lucros e almofada, limites efectivos do perfil, modo (exploração, probação, activo, pausa, disjuntor), linha de estado, ajustes do Doutor de Fluxo, investigação, símbolos em quarentena, a equipa e o conselheiro. `python mt5_bot.py --status` mostra-o formatado.

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

O que vale a pena guardar está na pasta da conta, `state\<servidor>_<login>\`: `mt5bot.sqlite` e `risk_state.json`. Com o bot a correr, **não copie o `.sqlite` com o Explorador** (pode apanhar uma escrita a meio). Use a cópia online do SQLite, que é segura com o bot ligado:

1. Crie a pasta de destino: `mkdir C:\mt5-backups`
2. Com o ambiente virtual activo:
   ```
   cd /d C:\mt5-bot
   python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" state\STARTRADER-Demo_12345678\mt5bot.sqlite C:\mt5-backups\mt5bot.sqlite
   ```
   **Sucesso:** o comando termina sem mensagens e o ficheiro aparece em `C:\mt5-backups`.
3. Copie também `state\STARTRADER-Demo_12345678\risk_state.json` e o seu `config.json`.

Para restaurar: pare o bot (Ctrl+C), copie os ficheiros de volta para a pasta da conta e arranque de novo. Os eventos do escritório são guardados durante 14 dias e os dossiers durante 365 (`team.events_retention_days`, `team.dossier_retention_days`). As actualizações de versão fazem a sua própria cópia de segurança em `backups\` (ver [docs/UPGRADE.md](docs/UPGRADE.md)).

### 15.8 Ficheiros de registo

| Ficheiro | Rotação | Conteúdo |
|---|---|---|
| `bot.log` | 2 MB x 5 | O bot (com segredos ocultados). |
| `supervisor.log` | 1 MB x 3 | Arranques, reinícios e o painel. |
| `panel.log` | 1 MB x 3 | O painel (ao lado do `bot.log`). |

Os ficheiros antigos (`bot.log.1`, ...) são apagados sozinhos. Para ver o fim em tempo real: `powershell -Command "Get-Content bot.log -Wait -Tail 30"`.

### 15.9 Actualizar de uma versão anterior

Siga [docs/UPGRADE.md](docs/UPGRADE.md): parar, copiar a pasta inteira, copiar os ficheiros novos, `pip install -r requirements.txt`, `pytest -q`, `python mt5_bot.py --migrate` (cópia de segurança e migração do estado mantendo todo o histórico), rever `config.migrated.json`, arrancar em seco. Para voltar atrás: `python mt5_bot.py --rollback-upgrade` e trocar as pastas.

---

## 16. Operações do dia a dia

| Quero... | Como | O que acontece às posições |
|---|---|---|
| Saber porque não negoceia | `python mt5_bot.py --diagnose --hours 24`, ou o separador **Porque não estou a negociar?** | Nenhum efeito. |
| Ver o estado | `python mt5_bot.py --status`, ou o painel. | Nenhum efeito. |
| Ver os braços e a evidência | `python mt5_bot.py --arms` | Nenhum efeito. |
| Ver os riscos do meu perfil | `python mt5_bot.py --risk-report` (estimativas Monte Carlo) | Nenhum efeito. |
| Parar com calma | **Ctrl+C** na janela do bot (ou do supervisor). | Ficam abertas, protegidas por SL/TP no servidor. |
| Parar e fechar tudo (emergência) | `cd /d C:\mt5-bot` e `type nul > KILL` | O bot fecha **as suas** posições e termina. O supervisor não o reinicia. |
| Voltar a usar depois do kill switch | `del KILL` e arranque de novo. | Enquanto o `KILL` existir, o bot fecha as posições e sai sempre que arrancar. |
| Pausar novas entradas sem parar | `"dry_run": true` no `config.json` (recarga automática), ou **Pausar novas entradas** no painel (secção 12.6), ou `type nul > state\<servidor>_<login>\PAUSE_ENTRIES`. | Continuam com SL/TP e gestão normal. |
| Desfazer uma mudança de nível | `python mt5_bot.py --history`, escolha o número da mudança e `python mt5_bot.py --rollback 7` | O braço volta ao nível anterior, fixo durante 24 horas (as descidas por evidência continuam). |
| Retomar depois do disjuntor de drawdown | Leia `state\<conta>\RESUME_REQUIRED.txt` e depois `python mt5_bot.py --resume` | O pico e o piso de lucros recomeçam no capital desse momento. |
| Recomeçar o piso de lucros | `python mt5_bot.py --rebase-floor` (fica na auditoria) | Nenhum efeito directo; o risco passa a contar a partir do capital actual. |
| Mudar de perfil de risco | `"risk_profile"` no `config.json`. Para um perfil mais conservador basta guardar; para um mais agressivo reinicie o bot. | As posições abertas mantêm o risco com que abriram. |

Com várias contas com estado, os comandos que lêem o estado usam a conta registada no `health.json`. Para escolher outra: `--account STARTRADER-Demo_12345678`.

**O disjuntor de drawdown:** se o capital cair mais do que o limite do perfil (12% no equilibrado, nunca mais de 20%) face ao pico, o bot fecha as suas posições, deixa de abrir novas, notifica e cria `RESUME_REQUIRED.txt`. Nem um novo dia, nem um reinício o desligam. Depósitos e levantamentos não contam como lucro nem como drawdown.

**O limite diário:** ao perder o limite diário do perfil desde o início do dia do servidor, o bot fecha as suas posições e pára até ao dia seguinte.

**O piso de lucros:** quando o capital toca o piso, as entradas novas param nesse dia; as posições mantêm SL/TP.

---

## 17. Estender o bot

Estratégias novas (um ficheiro), políticas de gestão, agentes e elementos do painel: [docs/EXTENDING.md](docs/EXTENDING.md). Uma estratégia nova entra como braços em sombra e só negoceia quando a evidência o justifica.

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


### 18.1 Outras variáveis

| Variável | Para quê | Como definir |
|---|---|---|
| `ANTHROPIC_API_KEY` | Chave do conselheiro Claude (secção 11.1). Alternativa: `python mt5_bot.py --set-claude-key` (Gestor de Credenciais do Windows). | `setx ANTHROPIC_API_KEY "sk-ant-..."` |
| `MT5BOT_PANEL_TOKEN` | Token das acções de controlo e do acesso remoto ao painel (secção 12.6). Use uma frase longa e aleatória. O nome pode mudar em `panel.token_env`. | `setx MT5BOT_PANEL_TOKEN "..."` |

Depois de `setx`, feche e abra um **novo** `cmd` e reinicie o supervisor. Nenhum destes valores é escrito em ficheiros, registos, bases de dados ou respostas do painel.

---

## 19. Investigação, backtest, simulação e relatórios

Nenhum destes comandos envia ordens.

* Um ciclo de investigação agora (dá aos braços uma expectativa inicial descontada; um bot em funcionamento lê-a no ciclo seguinte):
  ```
  python mt5_bot.py --research-once
  ```
  (`--optimize-once` continua a funcionar.)
* Relatório walk-forward fora da amostra de todas as estratégias, com os alertas de qualidade de cada uma:
  ```
  python mt5_bot.py --backtest
  ```
  Com `--offline` usa só o histórico já guardado.
* Uma estratégia com parâmetros fixos sobre todo o histórico (resultado **dentro da amostra**, não é validação):
  ```
  python mt5_bot.py --backtest --offline --strategy donchian --params "{\"channel\": 50, \"sl_mult\": 2.0, \"tp_mult\": 3.0}"
  ```
* Riscos do perfil (Monte Carlo, estimativas, inclui sempre o cenário sem vantagem):
  ```
  python mt5_bot.py --risk-report --days 250 --paths 2000
  ```
* Simulação do bot verdadeiro num mercado sintético, sem MT5, sem corretora e sem chave:
  ```
  python mt5_bot.py --simulate --sim-bars 600 --sim-speed 0
  ```
  Os dados ficam em `state_sim\`. Para ver a simulação no painel: `start_panel.bat --demo`. Nada aprendido aqui vale para mercados reais.
* Relatório de ablação da equipa: `python mt5_bot.py --ablation-report`. Recomeçar a aprendizagem da equipa: `python mt5_bot.py --reset-team-weights`.
* **Relatórios diários e semanais** (texto, CSV e HTML em português) em `reports\<servidor>_<login>\`, gerados sozinhos na mudança de dia. Incluem uma **auto-revisão**: funil de decisão, valor de cada filtro, deslizamento médio e tipos de saída, resultados por regime, políticas de gestão e mudanças, e depósitos ou levantamentos (excluídos do desempenho).

---

## 20. Resolução de problemas

Comece por `python mt5_bot.py --status` e `python mt5_bot.py --diagnose --hours 24`. Cada código de bloqueio, mensagem de arranque, estado do conselheiro, retcode da corretora e código de saída está explicado em [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

---

## 21. Executar os testes

Os testes substituem o pacote `MetaTrader5` por uma versão falsa e o conselheiro por um cliente falso: **nunca** tocam no terminal, na conta nem na API da Anthropic (não precisam de chave nem de rede) e correm também em Linux ou macOS.

```
cd /d C:\mt5-bot
.venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
```

**Sucesso:** a última linha diz `980 passed, 6 skipped in ...` (alguns minutos). Os ignorados são os testes lentos e os que precisam do pacote opcional `anthropic` (os testes do motor do escritório em Node.js são ignorados se o Node.js não estiver instalado). Os lentos (investigação completa em muitas sementes e o ciclo de vida ponta a ponta) correm com `pytest -q --runslow`.

O que é verificado, entre outros:

* **Segurança:** tectos de risco e comportamentos proibidos, SL e TP em todas as ordens (também com propostas aleatórias), tectos que nenhuma proposta ultrapassa, disjuntores, kill switch, Guardião, política de contas (demo, real, confirmação), uma entrada por símbolo, ordens divididas contadas como uma posição.
* **Evidência e níveis:** em 120 mercados sem vantagem no máximo 3 subidas falsas a T2 e nenhuma a T3; vantagens verdadeiras chegam a T3; histerese, cooldown, descidas imediatas, pins; orçamento de exploração nunca excedido; amostragem de Thompson.
* **Capital:** perfis, piso de lucros, almofada, anti-martingale, Kelly, risco aberto, contas de 50 a 1 000 000 com forex, ouro, índices e cripto, excepção do lote mínimo, depósitos e levantamentos.
* **Funil e Doutor de Fluxo:** cada portão deixa um código, o valor de cada filtro, ajustes limitados e revertidos, filtros que protegem nunca são relaxados, bloqueios raros nunca relaxam nada, `--diagnose` em português.
* **Estratégias:** contrato de todas as estratégias (determinismo, sem olhar para o futuro, limites de parâmetros); a sombra igual ao backtest.
* **Gestão, regimes e auto-cura:** repetição emparelhada das políticas, escolha com histerese, aplicação ao vivo, picos de spread, notícias; regime marcado em cada trade e alocação por regime; quarentena de símbolos com falhas de execução.
* **Unificação:** migração da 3.0 com cópia de segurança e todo o histórico, `--rollback-upgrade`, configuração antiga com avisos, um único ponto de entrada e uma única base de dados.
* **Equipa, conselheiro e painel:** como na 3.0 (autoridade, falhas do conselheiro = mesma decisão, segredos nunca saem, cabeçalhos de segurança, sem código gerado), mais os tokens da marca, tipos de letra e logótipo locais, todos os textos em português, e os testes Node.js do escritório (caminhos A* óptimos, planta com todos os lugares acessíveis, câmara).

### 21.1 Teste do painel no navegador

`tests\frontend_smoke.mjs` usa o Chrome sem janela através do protocolo DevTools, só com o Node.js 22 ou mais recente (sem pacotes npm). Com o painel em demonstração numa janela (`start_panel.bat --demo`), noutra:

```
node tests\frontend_smoke.mjs http://127.0.0.1:8765/ smoke "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

**Sucesso:** termina com código 0 e grava em `smoke\` as capturas e um `report.json` sem erros. Abre cada separador, segue um agente, abre uma ficha, usa a paleta, as preferências, o alto contraste, a visita guiada, o tema claro e o ecrã de telemóvel, e falha com qualquer erro de JavaScript, separador vazio ou deslocamento horizontal no telemóvel. Os testes unitários do motor do escritório: `node --test tests\js\office.test.mjs`.

---

## 22. Limitações e pressupostos

O que não foi verificado (o terminal MT5 real em Windows, a API verdadeira da Anthropic, o painel em Edge e Firefox, os dados reais de uma corretora), os pressupostos (preços BID, dia do servidor, capital de referência da sombra) e as limitações estão em [docs/LIMITATIONS.md](docs/LIMITATIONS.md). Em resumo: **nada garante lucro**; a exploração com micro risco custa os custos quando não há vantagem; resultados em sombra são estimativas; uma posição por símbolo por bot.
