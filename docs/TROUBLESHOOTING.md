# Resolução de problemas

Comece sempre por estes dois comandos, na pasta do projecto com o ambiente virtual activo:

```
python mt5_bot.py --status
python mt5_bot.py --diagnose --hours 24
```

O primeiro mostra o estado (capital, níveis dos braços, orçamento de exploração, piso de
lucros, bloqueios activos). O segundo mostra o funil de decisão e o bloqueio principal, em
português. O painel tem o mesmo no separador **Porque não estou a negociar?**.

## Não abre operações

| O diagnóstico diz | Significado | O que fazer |
|---|---|---|
| `OUTSIDE_HOURS` | Fora de `trading_hours_server` (hora do **servidor**). | Confirme a hora no Market Watch do MT5. Alargue a janela ou deixe o Doutor de Fluxo alargá-la (até 4 h, dentro de 1h a 23h). |
| `SPREAD_LIMIT` / `SPREAD_SPIKE` | Spread caro face ao stop, ou pico face ao normal daquela hora. | Normal perto do rollover e de notícias. Prefira símbolos com spread menor ou períodos maiores (stops maiores). |
| `MIN_LOT_EXCEEDS_RISK` | O lote mínimo arrisca mais do que o permitido para este capital. | Veja o mapa de viabilidade no painel (capital mínimo por símbolo e período). Use símbolos mais baratos (EURUSD em vez de ouro) ou períodos menores. |
| `EXPLORATION_BUDGET_SPENT` | O orçamento de exploração do dia ou da semana foi gasto. | Comportamento pretendido: recomeça no dia seguinte do servidor. |
| `THOMPSON_REJECT` | A evidência dos braços em sombra é fraca ou negativa. | Pretendido: o bot não explora braços que a evidência condenou. |
| `EXPLORATION_SLOTS_FULL` | Já há 2 trades de exploração abertos. | Espere que fechem, ou suba `tiers.exploration_slots`. |
| `ONE_PER_SYMBOL` | Já há uma posição do bot nesse símbolo. | Regra fixa: uma entrada por símbolo. |
| `TEAM_VETO`, `TEAM_LOW_CONFIDENCE`, `TEAM_NO_AGREEMENT` | A equipa vetou ou não concordou. | Abra o dossier no **Registo de decisões**. Com `team_mode: auto` a equipa só reduz depois de provar valor. |
| `DATA_UNHEALTHY` | A Escriturária de Dados viu velas em falta, preços parados ou ticks antigos. | Confirme a ligação e o histórico no MT5 (abra um gráfico do símbolo). |
| `HALTED_DAILY` | Limite de perda diária atingido. | Recomeça sozinho no dia seguinte do servidor. |
| `HALTED_DRAWDOWN` | Disjuntor de drawdown. | Leia `state\<conta>\RESUME_REQUIRED.txt` e depois `python mt5_bot.py --resume`. |
| `FLOOR_SOFT_STOP` | O capital tocou o piso de protecção de lucros. | Pára só nesse dia. Para começar uma nova época: `python mt5_bot.py --rebase-floor`. |
| `SYMBOL_QUARANTINED` | 3 falhas de execução seguidas nesse símbolo. | Pausa de 30 min (duplica até 8 h). Veja o `bot.log` para o motivo da corretora (retcode). |
| `TRADE_MODE_RESTRICTED` / `MARKET_CLOSED` | A corretora só permite fechar, ou o mercado está fechado. | Nada a fazer: volta quando a corretora reabrir. |
| `Nenhuma decisão registada nesta janela` | O bot não avaliou barras. | O bot está a correr? Veja `--status`, o fim do `bot.log` e se o MT5 está ligado. |

Se o diagnóstico mostrar sinais e nenhum bloqueio dominante, o funil tem a resposta linha a
linha no separador **Porque não estou a negociar?** (tabela de bloqueios, gráfico por hora).

## Arranque e ligação

| Sintoma / mensagem | Causa provável | Solução |
|---|---|---|
| `REAL MONEY account ... one deliberate confirmation` (código 3) | Conta real sem confirmação. | README, secção 8.2. |
| `account_mode is "demo_only" but ...` (código 3) | A conta não corresponde ao `account_mode`. | Mude de conta no terminal ou ajuste `account_mode`. |
| `mt5.initialize() failed: (-10003, 'IPC initialize failed ...')` | MT5 fechado ou a arrancar; Python de 32 bits; vários terminais; permissões diferentes. | Abra o MT5 e espere que ligue. Confirme 64 bits. Defina `mt5_path`. Corra os dois com o mesmo nível de permissões. |
| O terminal não abre sozinho | `terminal64.exe` fora de Program Files ou várias instalações. | `"mt5_path": "C:\\Program Files\\NOME DA PASTA\\terminal64.exe"`. |
| `Symbol 'EURUSD' not found (also tried broker suffixes)` | O símbolo tem outro nome ou não existe nessa conta. | Market Watch > Show All; copie o nome exacto para `symbols`. |
| `Unknown config key(s): ...` (código 2) | Chave escrita com erro, ou de outra versão. | A mensagem lista as chaves válidas. Chaves que começam por `_` são ignoradas (útil para comentários). |
| `config.json changed but is invalid; keeping the previous config` | Erro de escrita no ficheiro. | A mensagem diz a chave e o problema. O bot continua com a configuração anterior. |
| `... needs a restart; ignored` | Alargou um limite, escolheu um perfil mais agressivo ou mudou uma chave de reinício. | De propósito: reinicie o bot para aplicar. |
| `Restart circuit breaker` no `supervisor.log` | O bot falhou 5 vezes em 30 minutos. | Veja o fim do `bot.log`, corrija a causa e arranque `start_supervisor.bat`. |

## Investigação

| Sintoma | Causa | Solução |
|---|---|---|
| `Investigação: ... falhou: only N cached bars` | Histórico insuficiente no terminal. | MT5 **Tools > Options > Charts > Max bars in chart**: Unlimited (ou 100000). Abra um gráfico do símbolo em cada período. A investigação tenta de novo ao fim de 60 minutos. |
| Investigação lenta | Muitos símbolos x períodos x estratégias. | Corre em segundo plano, em baixa prioridade; não atrasa o bot. Reduza `universe.max_symbols` ou `trials_per_strategy` se o PC for fraco. |

## Painel

| Sintoma | Causa | Solução |
|---|---|---|
| O painel não abre | Não está a correr, ou outra porta. | Veja `panel.log`. Arranque com `start_panel.bat` e use o endereço que mostra. |
| `Port 8765 is already in use` | Outro programa usa a porta. | `start_panel.bat --port 8800`. Para ver quem usa: `netstat -ano \| findstr :8765`. |
| "Ainda não há dados" ou "O bot não actualiza o ficheiro de saúde" | O bot não está a correr, ou o painel vê outra conta. | Arranque o bot. Com várias contas: `start_panel.bat --state-dir state\<servidor>_<login>`. |
| "a consultar" em vez de "ao vivo" | A ligação ao vivo foi cortada (proxy, antivírus, suspensão). | O painel lê de 3 em 3 segundos e volta a ligar sozinho. |
| O escritório anda devagar | PC sem aceleração gráfica, ou muitos efeitos. | Preferências (botão ⚙): Efeitos "Poucos" ou "Desligados", ou Movimento "Reduzido". O monitor de desempenho mostra fps e ms por imagem. |
| A visita guiada aparece sempre | O navegador não guarda dados (modo privado). | Normal em modo privado. Feche com "Saltar"; ou abra o painel com `?tour=0`. |
| Botões de controlo desligados | `panel.control_actions_enabled` é `false` ou falta `MT5BOT_PANEL_TOKEN`. | README, secção 12. Depois de `setx`, abra um novo `cmd` e reinicie o supervisor. |

## Conselheiro Claude

| Estado | Causa | Solução |
|---|---|---|
| `NO_KEY` | A variável não chegou ao processo do bot. | `setx ANTHROPIC_API_KEY "..."`, feche todas as janelas `cmd` e arranque de novo; ou `python mt5_bot.py --set-claude-key`. |
| `OFFLINE_NO_CREDIT` | Crédito esgotado na conta Anthropic. | Carregue crédito. O bot testa ao fim de 30 min e depois com intervalos crescentes. Entretanto negoceia sem ele. |
| `OFFLINE_AUTH` | Chave inválida ou revogada. | Crie uma chave nova. O bot só volta a tentar com uma chave diferente. |
| `DEGRADED` | Sobrecarga ou rede. | Nada a fazer: tenta de novo com espera crescente. |
| `BUDGET_CAPPED` | Atingiu `max_calls_per_hour` ou `max_daily_cost_usd`. | Pretendido; retoma na janela seguinte. |
| `DISABLED` com `enabled: true` | Falta o pacote `anthropic`, ou `--no-claude`. | `pip install anthropic` no ambiente virtual. |

## Códigos de retorno da corretora (retcode)

| Código | Nome | O que fazer |
|---|---|---|
| 10004 | Requote | O bot repete até 3 vezes com preço novo. |
| 10006 | Request rejected | A corretora recusou. Se persistir, o símbolo entra em quarentena; contacte o suporte. |
| 10014 | Invalid volume | Volume fora do mínimo, máximo ou passo. Veja **Specification** no MT5. |
| 10015 | Invalid price | Mercado muito rápido ou símbolo sem cotação. |
| 10016 | Invalid stops | SL/TP perto demais. O bot respeita o `Stops level`; se acontecer, a corretora alterou-o. |
| 10018 | Market is closed | O bot espera 15 minutos antes de tentar de novo nesse símbolo. |
| 10019 | No money | Margem insuficiente. Reduza o risco ou o número de posições. |
| 10027 | AutoTrading disabled by client | Algo Trading desligado no terminal. |
| 10030 | Unsupported filling mode | O bot escolhe IOC, FOK ou RETURN conforme o símbolo; se persistir, informe a corretora. |

## Códigos de saída do bot

| Código | Significado |
|---|---|
| 0 | Saída normal (Ctrl+C, kill switch com posições fechadas, contagem cancelada). |
| 1 | Erro inesperado. O supervisor reinicia. |
| 2 | Falha no arranque (configuração, ligação, Algo Trading, símbolo). O supervisor reinicia com espera. |
| 3 | Paragem de segurança (conta real sem confirmação, `account_mode` errado, troca de conta). O supervisor **não** reinicia. |
| 4 | (supervisor) Disjuntor de reinícios. |
