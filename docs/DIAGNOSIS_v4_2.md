# Diagnóstico v4.2: porque é que a versão anterior não fez nenhum trade

Documento da fase 0. Separa o que é **facto** (lido no código ou nos números do painel), o que é **inferência** (derivado desses números) e o que **não foi possível verificar**.

## 0. Limite desta análise

Esta máquina não tem dados reais: não há `state/` da conta do proprietário, não há diário, não há `account_info`, não há MT5 e não há SQLite de produção. O único estado existente é `state_demo/`, de uma simulação sintética com 10 000 unidades. Por isso:

* o capital, a moeda e a alavancagem **não foram lidos** de `account_info`. O capital abaixo é uma inferência a partir dos números do painel, comparada com as constantes do código;
* a moeda e a alavancagem continuam desconhecidas. Assume-se USD porque o orçamento aparece em USD;
* a repetição do dia gravado do proprietário **não foi possível**: nem o registo de eventos nem o histórico MT5 dessas datas existem aqui. A secção 5 explica o que foi feito em vez disso.

Quando correr `preflight.bat` na máquina Windows, o capital real substitui a inferência.

## 1. Análise corrigida

### 1.1 Corrigido: o capital é cerca de 65 USD, não 100 a 110 USD

A análise de partida inferiu 100 a 110 USD a partir de um limite T1 de cerca de 0,135 %. Esse número não existe no código. O mapa de acessibilidade é calculado em `opportunity.affordability_rows`, com `min_equity = risco do lote mínimo / risco do nível`. Logo, em todas as linhas:

```
capital mínimo / risco do lote mínimo em % = capital / risco T1 em %
```

Nas nove linhas do painel essa razão fica entre 813,2 e 814,2 (média 813,6). Para `aggressive` em conta real, o código define T1 = 0,08 %, orçamento diário 0,15 % e semanal 0,50 %. Isto dá:

| hipótese | capital | orçamento diário | orçamento semanal | painel: 0,10 / 0,33 |
|---|---|---|---|---|
| **aggressive, conta real** | **65,09** | **0,098** | **0,325** | **confere nos dois** |
| aggressive, conta demo | 81,36 | 0,244 | 0,651 | não confere |
| balanced, conta real | 40,68 | 0,041 | 0,122 | não confere |

Há uma segunda verificação, independente. Com 65 USD, a distância de stop implícita para EURUSD H1 é 23,6 pips, ou seja um ATR de 15,7 pips, plausível. Com 110 USD seria 39,9 pips e um ATR de 26,6 pips, invulgarmente alto para EURUSD H1.

**Inferência:** capital de cerca de 65,09 USD, conta real, perfil `aggressive`.

Os valores 0,10 e 0,33 parecem 0,1 % e 0,3 % de 100 a 110 USD porque são os percentuais do perfil `balanced`. Foi uma coincidência numérica.

### 1.2 Confirmado: era matematicamente impossível negociar

Com 65,09 USD:

* o orçamento diário de exploração era 0,098 USD;
* o trade T1 mais barato (EURUSD M5, 0,01 lote) arriscava 0,413 USD, mais de quatro vezes o orçamento do dia inteiro;
* o limite T1 permitia 0,052 USD por trade, e o limite por trade de exploração 0,098 USD.

Nenhuma espera podia resolver isto. O Doutor de Fluxo também não, porque a sua alavanca para `MIN_LOT_EXCEEDS_RISK` era `universe_extra`, que acrescenta símbolos com o mesmo problema.

### 1.3 Corrigido: o perfil `aggressive` teve efeito na exploração

A análise de partida dizia que o perfil não chegava a T1 nem ao orçamento. O código mostra o contrário: `effective_limits` aplica-o e os valores do painel só conferem com `aggressive`. O problema real é outro: os números do perfil eram demasiado pequenos para ter qualquer efeito numa conta deste tamanho, e os tectos `MAX_EXPLORATION_TRADE_PCT` (0,25 %) e `MAX_EXPLORATION_DAY_PCT` (0,5 %) impediam que `aggressive` chegasse ao lote mínimo, mesmo que os números fossem maiores.

### 1.4 Novo: havia um segundo bloqueio, escondido atrás do primeiro

Isto não constava da análise de partida e é a descoberta mais importante desta fase. **Remover só o bloqueio do lote mínimo não teria produzido nenhum trade.**

`RiskManager.approve_entry` recusa qualquer entrada cuja exposição nocional ultrapasse `max_symbol_exposure_x` (5,0 por omissão, tecto de código 10,0). 0,01 lote de EURUSD tem cerca de 1 160 USD de nocional:

| capital | nocional / capital | limite 5x | tecto 10x |
|---|---|---|---|
| 65 USD | 17,8x | recusado | recusado |
| 110 USD | 10,5x | recusado | recusado |
| capital mínimo | | 232 USD | 116 USD |

Com a configuração por omissão, qualquer conta abaixo de 232 USD ficava bloqueada no lote mínimo de EURUSD. O sintoma nunca apareceu porque o sinal morria antes, na etapa de acessibilidade. A correcção está no ADR-019.

### 1.5 Novo: outros bloqueios e dependências de saldo

* **Anti-martingale em percentagem.** O limite guardava a percentagem do último trade perdedor. Depois de uma perda no lote mínimo, o capital desce, a mesma perda em dinheiro passa a ser uma percentagem maior e o braço ficaria bloqueado até ganhar, o que nunca aconteceria. Agora compara dinheiro no tamanho mínimo.
* **Porta de custo.** Exige `k = 2` sobre o limite inferior do intervalo, a regra certa para aumentar tamanho, mas demasiado dura para entrar no tamanho mínimo. Em conta real a exploração não estava isenta (`exploration_exempt_real: false`), e como o limite inferior de um braço sem evidência é negativo, a exploração real nunca podia passar. Era um terceiro bloqueio. Daí a separação entre portas de entrada e de escala.
* **Bandas fixas em dinheiro no código.** `LARGE_ACCOUNT_REF = 100 000` em `capital.py` e `small_account_equity = 25 000` em `core/tail.py` e `bot_oversight.py`. Ambas contrariam o objectivo de não haver limites fixos de saldo. Foram removidas.
* **Limite por moeda.** `portfolio.max_currency_risk_pct = 2,0` faz com que EURUSD e BTCUSD no lote mínimo, que partilham USD, não possam estar abertos ao mesmo tempo. Foi mantido: encolhe risco e é correcto numa conta pequena.

### 1.6 Confirmado: o universo não era guiado pela acessibilidade

XAUUSD era inviável em todas as linhas e continuava no universo. Os candidatos eram avaliados só no arranque, por isso um depósito não alterava nada sem reiniciar. As chaves antigas `symbols` e `timeframe` coexistiam com `universe`.

### 1.7 Confirmado: o stop era fixo, não uma decisão

O mapa usava sempre 1,5 ATR e os braços por omissão usavam o multiplicador da estratégia (2,0 em geral). A distância do stop determina a viabilidade e o custo em R, e tem de ser escolhida em conjunto com eles.

### 1.8 Confirmado: a qualidade dos sinais também é um problema

Os 22 sinais bloqueados teriam feito, em média, -0,50R. Com 22 amostras o erro padrão ronda 0,25R a 0,30R, portanto é evidência fraca mas real, e aponta contra desbloquear tudo.

**Consequência em dinheiro:** com 65 USD e trades de 0,6 a 1,8 % de risco, -0,50R por trade em quatro trades por dia custa cerca de 1 a 2 USD por dia, ou seja 1,5 a 3 % do capital por dia. Uma semana assim atingiria o limite semanal. O desbloqueio só faz sentido com selecção por valor esperado e com as portas de entrada, e é possível que, com os dados reais, nenhum braço passe as portas. Nesse caso o sistema **não deve** negociar.

### 1.9 Confirmado: a evidência acumulava devagar

Só o braço por omissão de cada estratégia, nos símbolos do universo, era seguido em sombra. Os sinais bloqueados já eram seguidos em sombra pelo seu braço, por isso o ciclo contrafactual existia; faltavam variantes de stop, os símbolos inviáveis e o uso imediato da evidência.

## 2. Viabilidade com os números do proprietário

Percentagem do risco do lote mínimo sobre o capital. Stop típico de 1,5 ATR, como no painel. Limite `aggressive`: 2,0 %.

| símbolo | tf | risco (USD) | % com 65,09 | % com 110 | viável até 2 % com 65,09 |
|---|---|---|---|---|---|
| EURUSD | M5 | 0,413 | 0,635 | 0,376 | sim |
| EURUSD | M15 | 0,983 | 1,510 | 0,893 | sim |
| EURUSD | H1 | 2,361 | 3,628 | 2,147 | não |
| BTCUSD | M5 | 0,735 | 1,130 | 0,669 | sim |
| BTCUSD | M15 | 1,207 | 1,854 | 1,097 | sim |
| BTCUSD | H1 | 4,249 | 6,528 | 3,863 | não |
| XAUUSD | M5 | 6,226 | 9,565 | 5,660 | não |
| XAUUSD | M15 | 12,446 | 19,122 | 11,315 | não |
| XAUUSD | H1 | 26,675 | 40,983 | 24,250 | não |

Estes valores são consistentes com a inferência da secção 1.1, mas dependem dela. A conclusão sobre quais são viáveis não depende de ser 65 ou 110, excepto EURUSD H1 e BTCUSD H1, que continuam inviáveis nos dois casos.

## 3. Consequência para o desenho

1. O risco tem de ser medido em relação à granularidade da corretora, não só em percentagem.
2. O orçamento tem de pagar pelo menos alguns trades ao tamanho mínimo.
3. A exposição nocional no lote mínimo precisa de um tecto próprio, derivado da alavancagem que a própria regra admite.
4. Viabilidade, custo e stop são uma só decisão.
5. Desbloquear sem seleccionar perde dinheiro: portas de entrada e valor esperado são obrigatórios.

## 4. Comissão

O excerto de configuração indica `commission_fallback_per_lot: 7.0`. O código usava `commission_per_lot` sem o comparar com o histórico. Agora o histórico real tem prioridade e qualquer divergência fica no registo. **Não verificado aqui:** sem negócios reais não se sabe se a conta paga comissão. Os valores da secção 2 podem incluir 7 por lote de comissão, caso em que os stops implícitos são cerca de 0,7 pip menores do que os indicados.

## 5. O dia gravado do proprietário

**Não foi repetido, porque os dados não existem nesta máquina.** Foi feito em vez disso:

* `tests/test_v42_replay.py` reproduz uma sessão **sintética** com as especificações e distâncias de stop do proprietário, com capital de 65 e de 110 USD. Prova o mecanismo: a v4.2 coloca ordens onde a versão anterior bloqueava 100 %. Não diz nada sobre o resultado real;
* o resultado em R dessa sessão é sintético e não deve ser lido como previsão;
* `python mt5_bot.py --replay-day AAAA-MM-DD` corre a mesma repetição sobre as velas em cache na máquina Windows, depois de o bot ter descarregado o histórico. É assim que se obtém o número real.
