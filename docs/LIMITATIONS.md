# Limitações e pressupostos

## O que não foi verificado

O código foi desenvolvido e testado em Linux, com um MT5 simulado, um cliente da Anthropic
falso e um mercado sintético. **Não foi verificado:**

* a interacção real com o terminal MT5 e a STARTRADER (abertura automática do
  `terminal64.exe`, reinício de um terminal congelado, campos devolvidos pela corretora como
  `trade_tick_value_loss`, `swap_mode`, `margin_mode`, comissões);
* os scripts `.bat` e `.ps1` em Windows reais, incluindo o arranque automático e o
  `PanelKeeper` do supervisor;
* a baixa prioridade dos processos de investigação no Windows;
* a API verdadeira da Anthropic (o formato dos pedidos foi verificado com o SDK oficial contra
  um servidor local); se a Anthropic mudar algo, o pior caso é o conselheiro ficar indisponível;
* o painel em Edge, Firefox ou Safari, e com o servidor do painel em Windows (testado no Chrome
  sem janela em Linux, incluindo tema claro, alto contraste e ecrã de telemóvel);
* os 60 fps do escritório em hardware real: no Chrome sem aceleração gráfica cada imagem
  levou 3 a 11 ms (o orçamento para 60 fps é 16,7 ms), mas o Chrome sem janela limita a
  cadência a cerca de 30 imagens por segundo, por isso a medida directa de fps não foi possível;
* a evidência, os níveis, o Doutor de Fluxo e a equipa com dados reais de uma corretora: só
  foram exercitados em simulação e em testes, onde nada prova valor em mercados reais.

## Pressupostos

* As velas do MT5 são preços BID. Em sombra e na investigação a compra paga o spread da
  vela e as saídas de vendas usam o ASK. Dentro da mesma vela, se o SL e o TP forem tocados,
  conta o SL (pessimista).
* O "dia" dos limites diários e do orçamento de exploração é o dia do **servidor** da
  corretora. O drawdown e o limite diário usam o capital de toda a conta (incluindo operações
  manuais ou de outros robôs). Depósitos e levantamentos são detectados e excluídos.
* A sombra e a investigação usam um capital de referência fixo (10 000, risco 1%): os
  resultados em R não dependem do tamanho da conta.
* A evidência assume que os resultados de um braço são aproximadamente independentes e que o
  passado recente (meia-vida de 120 dias) é informativo. Mudanças de regime bruscas podem
  enganar a evidência durante algum tempo; as descidas de nível são imediatas para limitar isso.
* O regime de cada trade usa a mesma classificação que o Detector de Regime (eficiência,
  ADX, percentil de ATR, volume), calculada só com velas fechadas.
* A API Python do MT5 não fornece horários de sessão nem calendário económico: o mercado é
  considerado aberto quando há ticks a mudar; o filtro de notícias usa um CSV mantido por si.
* Swaps positivos são ignorados (pessimista). Comissões: detectadas no histórico; sem
  histórico usa 7 por lote. Em contas só com spread defina `"commission_per_lot": 0`.
* Contas "contest" são tratadas como demo. Python 3.10 a 3.12, 64 bits.

## Limitações

* **Nada garante lucro.** Mais exploração significa mais trades de micro risco, e trades de
  exploração sem vantagem perdem os custos. O orçamento limita essa perda; não a elimina.
* Resultados em sombra e na investigação são estimativas. O deslizamento real no SL, gaps de
  fim de semana e de notícias e alterações de execução podem fazer uma perda real exceder 1R.
* A latência de um PC em casa é maior do que a de um servidor junto da corretora; os braços M5
  sofrem mais com isso.
* Uma posição por símbolo por bot. Não corra duas instâncias com o mesmo `magic`.
* O painel mostra dados com alguns segundos de atraso e não substitui o terminal MT5 para ver
  o estado real da conta.
* A equipa e o conselheiro não criam vantagem: só reduzem ou vetam. Também podem recusar
  operações que teriam sido lucrativas (o funil mede quanto).
* Os custos da API da Anthropic são da sua responsabilidade.
