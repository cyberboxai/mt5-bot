# Evidência, níveis de risco e protecção do capital

Este documento explica como o bot decide **quanto** arriscar: com que evidência um braço
passa a negociar, quanto arrisca em cada nível, como explora com micro risco desde o
primeiro dia, como protege os lucros e como se adapta a contas de 50 a 1 000 000.

> Nada aqui garante lucro. O sistema dimensiona o risco pela evidência que tem e fica de
> fora quando a evidência é contra. Com uma estratégia sem vantagem, o resultado esperado é
> uma perda pequena e limitada (custos), não um ganho.

## 1. Braços

Um **braço** é uma combinação concreta: estratégia + parâmetros + símbolo + período
(por exemplo `donchian(channel=30, sl_mult=2, tp_mult=3) EURUSD M15`). Cada braço tem o seu
livro em sombra (trades em papel, nunca enviados), a sua evidência e o seu nível.

* No arranque o bot cria um braço com os parâmetros por omissão para cada estratégia, em cada
  símbolo e período do universo (até `universe.max_arms`, 400).
* A investigação walk-forward (`--research-once`, ou sozinha em segundo plano) acrescenta
  braços com os melhores parâmetros fora da amostra e dá-lhes uma expectativa inicial.
* Uma posição aberta fica sempre com o braço que a abriu: as regras de saída e a política de
  gestão são as desse braço, mesmo que outro braço suba de nível entretanto.

## 2. Evidência

Para cada braço o bot calcula uma distribuição posterior da expectativa por trade, em R
(1R = a perda até ao stop):

1. **Amostra própria:** cada sinal conta uma vez. Se houve trade real, conta o real; senão o
   da sombra. Trades antigos pesam menos (meia-vida de 120 dias).
2. **Investigação descontada:** os trades fora da amostra da investigação entram com peso
   0,3 (`tiers.prior_discount`): ajudam a começar, não decidem sozinhos.
3. **Partilha hierárquica:** braços da mesma família de estratégia partilham informação; um
   braço com pouca evidência fica perto da média da família, e a média da família fica perto
   de uma média global céptica de **-0,03R** (`tiers.global_mean`: sem evidência, o bot
   assume que os custos tornam a expectativa ligeiramente negativa). A média da família de cada
   braço é calculada **sem** esse braço, para que um braço com sorte não se empurre a si próprio.
4. **Resultado:** média, desvio, intervalo de 90% e **P(E>0)**, a probabilidade de a
   expectativa ser positiva.

O separador **Estratégias e Investigação** do painel e `python mt5_bot.py --arms` mostram estes
números para cada braço.

## 3. Níveis

| Nível | Nome | Risco por trade (perfil equilibrado) | Entra quando | Sai quando |
|---|---|---|---|---|
| T0 | sombra | 0 (só papel) | ponto de partida | |
| T1 | probação | 0,10% demo, 0,05% real | P(E>0) >= 0,55 com pelo menos 20 trades de evidência própria, **ou** por exploração (secção 4) | P(E>0) < 0,50 |
| T2 | activo | 0,35% | P(E>0) >= 0,80 e pelo menos 50 trades | P(E>0) < 0,70, ou perda desde a última mudança > 6R |
| T3 | comprovado | até 1,00% (Kelly fraccionado) | P(E>0) >= 0,93, pelo menos 100 trades e drawdown <= 12R | P(E>0) < 0,85 |

* **Uma subida de cada vez** e no máximo uma a cada 4 horas (`tiers.promote_cooldown_hours`).
* **As descidas são imediatas**, sem cooldown.
* A diferença entre entrar e sair (histerese) impede o braço de oscilar entre níveis.
* Cada mudança fica na auditoria com as métricas que a justificaram (`--history`).
  `--rollback ID` repõe o nível anterior e fixa-o por 24 horas (as descidas por evidência
  continuam a aplicar-se).

**Porque estes números.** 0,55 é "ligeiramente mais provável ganhar do que perder" e só dá
micro risco. 0,80 com 50 trades e 0,93 com 100 trades foram calibrados em simulação: em 120
mercados sem vantagem, no máximo 3 braços chegaram a T2 por acaso e nenhum a T3; vantagens
verdadeiras de +0,35R chegaram a T3 (`tests/test_evidence_tiers.py`).

## 4. Exploração: negociar desde o primeiro dia, com micro risco

Um braço em T0 com sinal pode fazer **um trade de exploração**:

1. A sua expectativa é sorteada da posterior (amostragem de Thompson). Braços incertos mas
   plausíveis ganham mais sorteios; braços que a evidência condenou quase nunca.
2. Se o sorteio for positivo e houver uma vaga livre (2 por omissão,
   `tiers.exploration_slots`), o trade segue com o risco de T1.
3. O dinheiro em risco sai de um **orçamento de exploração** por dia e por semana (dia do
   servidor). Trades ganhos devolvem orçamento; nunca mais do que o orçamento inicial.

| Orçamento de exploração | Conservador | Equilibrado | Agressivo | Tecto no código |
|---|---|---|---|---|
| Demo, por dia / semana | 0,15% / 0,40% | 0,20% / 0,50% | 0,30% / 0,80% | 0,5% / 1,5% |
| Real, por dia / semana | 0,07% / 0,20% | 0,10% / 0,30% | 0,15% / 0,50% | 0,5% / 1,5% |
| Máximo por trade | igual ao orçamento diário, até 0,25% | | | 0,25% |

Com o orçamento gasto, o funil regista `EXPLORATION_BUDGET_SPENT` e a exploração recomeça no
dia seguinte. Na simulação a primeira ordem de exploração aparece poucos minutos depois do
arranque.

## 5. Perfis de risco

`"risk_profile"` no `config.json` (ou `--risk-profile` numa execução):

| | Conservador | Equilibrado (omissão) | Agressivo | Tecto no código |
|---|---|---|---|---|
| Risco T1 demo / real | 0,05% / 0,03% | 0,10% / 0,05% | 0,10% / 0,08% | |
| Risco T2 | 0,25% | 0,35% | 0,50% | |
| Risco T3 (máximo) | 0,50% | 1,00% | 1,75% | 2,0% por trade |
| Perda diária máxima | 1,5% | 2,5% | 4,0% | 5,0% |
| Risco aberto total | 1,5% | 2,5% | 3,5% | 4,0% |
| Disjuntor de drawdown | 8% | 12% | 18% | 20% |
| Piso de lucros abaixo do capital inicial | 5% | 8% | 12% | |
| Parte de cada novo máximo protegida | 50% | 40% | 30% | 90% |
| Posições abertas | 3 | 4 | 5 | 5 |
| Fracção de Kelly (T2/T3) | 0,15 | 0,25 | 0,35 | 0,5 |

Os limites efectivos são sempre o **mínimo** entre o perfil, as chaves que escrever no
`config.json` (por exemplo `risk_per_trade_pct_real`, `max_daily_loss_pct_demo`,
`max_total_open_risk_pct`, que só podem apertar) e os tectos rígidos de `mt5bot/limits.py`,
que nenhuma configuração ultrapassa. Mudar o perfil para um mais agressivo com o bot a correr
é recusado na recarga automática: exige reiniciar, de propósito.

Antes de escolher, veja os números em simulação:

```
python mt5_bot.py --risk-report
python mt5_bot.py --risk-report --risk-profile conservative --days 250 --paths 5000
```

O relatório mostra, para três cenários (sem vantagem, vantagem pequena, vantagem moderada),
a probabilidade de o disjuntor disparar, de tocar o piso, o drawdown típico e o pior, o
resultado mediano e o tempo de recuperação. São **estimativas** de um modelo simplificado.

## 6. Como se calcula o tamanho de cada trade

O risco de um trade é o **menor** destes valores (o painel e o funil dizem qual foi o
limitador):

1. o tecto rígido por trade (2,0%);
2. o risco do nível (T1, T2 ou T3) no perfil;
3. em T2/T3, a fracção de Kelly da posterior (com uma expectativa fraca, o risco desce);
4. em exploração, o orçamento que resta e o máximo por trade;
5. a distância ao piso de lucros (secção 7);
6. o risco aberto total que ainda cabe (todas as posições juntas);
7. anti-martingale: depois de uma perda o risco do braço nunca sobe; só volta a subir depois
   de um ganho;
8. correlação com posições abertas (reduz se for alta);
9. liquidez e contas muito grandes (secção 8).

Depois disso, a equipa de agentes e o conselheiro Claude **só podem reduzir** este valor.
O volume é arredondado **para baixo** ao passo do símbolo. Todas as ordens levam SL e TP.

## 7. Protecção dos lucros (piso que só sobe)

O bot mantém um **piso** de capital:

* começa em `capital inicial x (1 - piso%)` (perfil equilibrado: 8% abaixo);
* sobe com cada novo máximo: uma parte do lucro novo fica protegida (equilibrado: 40%);
* nunca desce sozinho.

O **risco disponível** é o capital acima do piso (a "almofada", visível na barra de topo do
painel). Perto do piso o risco por trade encolhe; quando a almofada é grande o risco pode
crescer até `house_money_max` (1,25x no equilibrado) com dinheiro já ganho, nunca acima dos
tectos. Ao tocar o piso as entradas novas param nesse dia (disjuntor suave); as posições
abertas mantêm SL/TP.

Exemplo (equilibrado, conta de 10 000): piso inicial 9 200. O capital sobe a 11 000: o lucro
novo é 1 000, ficam protegidos 400, o piso passa a 9 600. A almofada é 1 400.

`python mt5_bot.py --rebase-floor` começa uma nova época: o piso recomeça a partir do
capital actual (fica na auditoria). Depois de um `--resume` do disjuntor de drawdown o piso
também recomeça.

**Depósitos e levantamentos** são detectados no histórico de operações e nunca contam como
lucro, perda, drawdown ou disparo de limites: o capital inicial, o máximo e o piso ajustam-se
ao movimento.

## 8. Do tamanho da conta: de 50 a 1 000 000

O bot verifica, para cada símbolo e período, se o **lote mínimo** da corretora cabe no
risco permitido:

* **cabe:** negoceia normalmente;
* **não cabe, mas o risco do lote mínimo fica abaixo do tecto de excepção** (o orçamento de
  exploração em T1; `capital.affordability_cap_pct`, igual ao risco T3 do perfil, em T2/T3):
  aceita o lote mínimo como **excepção consciente**, registada no funil
  (`MIN_LOT_EXCEPTION`) e no trade;
* **não cabe:** não negoceia esse símbolo nesse período (`MIN_LOT_EXCEEDS_RISK`) e o mapa de
  viabilidade do painel diz o capital mínimo necessário.

Exemplo calculado com o código (conta demo, perfil equilibrado, lote mínimo 0,01, comissão
7 por lote, stop típico de 1,5 ATR em M15: 12 pips no EURUSD, 4,5 USD no ouro). Os stops são
pressupostos para ilustrar; os reais dependem do mercado.

| Capital | EURUSD T1 | EURUSD T2 | XAUUSD T1 | XAUUSD T2 |
|---|---|---|---|---|
| 50 | não (2,54%) | não | não (9,14%) | não |
| 200 | não (0,64%) | excepção | não (2,29%) | não |
| 1 000 | excepção (0,13%) | sim | não (0,46%) | excepção |
| 10 000 | sim (0,013%) | sim | sim (0,046%) | sim |
| 100 000 e 1 000 000 | sim | sim | sim | sim |

(Entre parênteses: o risco do lote mínimo em percentagem do capital.)

Em contas grandes:

* acima de 100 000 o risco por trade é reduzido 15% por cada ordem de grandeza
  (1 000 000: 0,85x), para não mexer o mercado em símbolos menos líquidos;
* se o volume calculado for maior do que o máximo da corretora por ordem, é dividido em até 4
  ordens (`MAX_SPLIT_ORDERS`), todas com SL e TP, contadas como **uma** posição para os limites.

## 9. Gestão das posições abertas

Cada braço tem uma **política de gestão**, escolhida por evidência:

| Política | O que faz |
|---|---|
| `fixed` (omissão) | SL e TP como colocados |
| `breakeven` | a partir de +1R o stop passa para o preço de entrada |
| `trail_atr` | a partir de +1R o stop segue o preço a 2 ATR (só aperta) |
| `time_stop` | fecha ao fim de 24 barras |

Cada trade em sombra é repetido sob todas as políticas, com as mesmas entradas e os mesmos
custos (comparação emparelhada). Um braço só troca de política quando a nova é melhor com
probabilidade >= 0,6 em pelo menos 30 trades (`management.*`); com poucos trades usa a
evidência da mesma estratégia e período noutros símbolos. Todas as políticas só apertam o
stop ou fecham mais cedo: nenhuma aumenta o risco.

Também:

* **picos de spread:** enquanto o spread estiver acima de 2,5x o normal daquela hora, os
  ajustes de stop esperam (um stop mais apertado seria apanhado pelo spread, não pelo mercado);
* **notícias:** 15 minutos antes de uma notícia de alto impacto, posições com pelo menos
  +0,5R passam o stop para o preço de entrada (`management.news_derisk_minutes`).

## 10. Regimes de mercado

Cada trade (sombra e real) fica marcado com o regime da barra do sinal: tendência, lateral,
volatilidade alta ou ilíquido. Na escolha entre sinais da mesma barra:

* os braços são ordenados por nível e depois pela P(E>0) **no regime actual** (a evidência
  do braço nesse regime, puxada para a sua média geral quando há poucos trades);
* um braço com pelo menos 20 trades no regime actual e P(E>0) < 0,25 nesse regime fica de
  fora nessa barra (`REGIME_INELIGIBLE`).

## 11. O Doutor de Fluxo

O Doutor de Fluxo (Flávio, no escritório) vigia o funil. Se o bot estiver parado há mais de
6 horas em demo (12 em real) com mercados abertos e sinais a aparecer, olha para o bloqueio
principal e, **se esse bloqueio for relevante** (pelo menos 5 vezes e 10% dos bloqueios) e
**se os sinais que bloqueia não teriam perdido dinheiro** (resultado em sombra), relaxa um
passo de um destes ajustes, dentro de limites fixos:

| Ajuste | Passo | Limite |
|---|---|---|
| horas extra no horário | 1 h | +4 h, nunca fora de 1h a 23h do servidor |
| tolerância a picos de spread | +0,25 | +1,0 |
| limiar P(E>0) de entrada em T1 | -0,01 | -0,03 (nunca abaixo de 0,52) |
| concordância mínima da equipa | -1 | nunca abaixo de 1 |
| confiança agregada mínima da equipa | -0,01 | -0,02 (nunca abaixo de 0,50) |
| símbolos extra no universo | +1 | +6 (só se houver candidatos viáveis) |

Cada ajuste é revisto depois de 10 trades ou 48 horas e revertido se esses trades tiverem
média abaixo de -0,25R. Se a conta piorar (60% da perda diária usada), aperta. **Nunca mexe
em risco por trade, limites diários, piso, disjuntores, SL/TP ou tectos.**

## 12. O que nunca muda

* Todas as ordens levam SL e TP no servidor.
* Os tectos de `mt5bot/limits.py` não são configuráveis.
* Uma entrada por símbolo; disjuntor de drawdown, limite diário, kill switch, Guardião.
* A equipa e o conselheiro só reduzem ou vetam.
* Nenhum código é gerado ou descarregado em tempo de execução.
