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

### Novo na versão 4.0: também não verificado

* **Nada da execução da 4.0 correu contra um terminal real.** A máquina de estados, a reconciliação e a verificação pós-preenchimento foram provadas com simulação de falhas, não com a STARTRADER. As assinaturas de `order_send`, `history_deals_get` e `account_info` e a lista de retcodes foram confirmadas na documentação oficial do MQL5 em 2026-11-01.
* **A documentação não diz o que `order_send` faz em todos os casos.** Não indica quando devolve `None` nem o limite do comentário. O bot trata `None` como incerto e usa 16 caracteres. Não está confirmado se a STARTRADER preserva o comentário em todas as ordens. Se não preservar, a reconciliação cai para a adopção por símbolo, lado e magic.
* `history_deals_get` devolver `None` em vez de vazio quando não há negócios é tratado como vazio só se `last_error()` indicar sucesso. Isto vem de experiência, não da documentação.
* **Não há relatório de realidade.** Não existem preenchimentos reais nesta máquina. O modelo de custos arranca de valores iniciais e todas as distâncias à realidade reais estão por medir.
* **Nenhum veredicto sobre braços reais.** Só foram usados dados sintéticos. Nenhuma estratégia desta versão tem vantagem demonstrada, e isto inclui `ts_momentum` e `asian_range`.
* Os Message Batches e a API da Anthropic não foram chamados a sério: sem chave nesta máquina. O formato segue a referência oficial do SDK e foi testado com um cliente falso.
* O bloco `--shadow` não correu com cotações reais. Os ficheiros `requirements.lock` foram gerados em Linux para todas as plataformas, e não foram instalados em Windows.
* A consulta NTP foi testada só no caminho de falha.

### Novo na versão 4.2: também não verificado

* **O capital do proprietário é uma inferência.** Cerca de 65 USD, deduzido dos números do painel. Moeda e alavancagem não foram lidas. `preflight.bat` dá os valores reais.
* **O dia gravado não foi repetido.** Os dados não existem aqui. Os testes usam uma sessão simulada; `--replay-day` corre o mesmo código em Windows. Só mostra o mecanismo, e não diz nada sobre o resultado real.
* **Não sabemos se algum braço passa as portas de entrada com dados reais.** Se nenhum passar, o bot só explora dentro do orçamento, ou não negoceia. Em 22 sinais a média foi -0,50R: é evidência fraca, mas contra.
* **Os preços de BTCUSD e XAUUSD no cálculo do piso são suposições.** Afectam a parte da exposição, não a do risco.
* **`order_calc_profit`, `order_calc_margin` e `volume_limit`** foram testados com o terminal simulado.
* **A capacidade por liquidez é uma heurística.** O MT5 só dá contagem de ticks em FX. `capacity.lots_per_tick` não foi calibrado com ordens reais. O tecto de capacidade é uma estimativa.
* **O envio de ordens filhas ao longo do tempo** nunca correu num terminal real.
* **Os lançadores `.bat` e `.ps1` novos** foram verificados por texto, não executados em Windows.
* **O custo de 1400 braços por barra** não foi medido em hardware real.
* **O Monte Carlo do relatório de risco** é simplificado: trades independentes, sem gaps, payoff fixo.

### Limitações novas da 4.2

* **30x de exposição no lote mínimo é muita alavancagem.** Com 65 USD, 0,01 lote de EURUSD são cerca de 18x. Um gap que atravesse o stop pode custar muito mais do que 2 %. Os limites de risco assumem que o stop executa perto do preço.
* **A porta de custo é exigente nos stops curtos.** Num stop de cerca de 4 pips em M5 o custo ronda 0,3R, e a razão de 1,2 pede cerca de 0,36R de vantagem depois de custos. Poucos braços devem passar nesses stops: a viabilidade não significa que vá haver trades.
* **Três perdas num dia atingem a perda diária.** Com cerca de 65 USD a variância domina: uma semana má pode levar ao limite semanal sem que nada esteja errado.
* **A meta-etiquetadora ainda não entra no valor esperado.** Continua no estágio da equipa.
* **Os filtros de confirmação só influenciam o EV depois de validados** (100 ou mais trades e 95 % de confiança). Até lá são medidos, sem efeito.
* **Actualização online com até um minuto de atraso**, na passagem de manutenção seguinte (ADR-026).
* **Ordens filhas perdem-se num reinício** (ADR-028).

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

* **Um kernel, mas com uma excepção.** A investigação usa `backtest.simulate`, mais rápido, e não o `Bot` inteiro (ADR-002).
* **O simulador trabalha por barras.** Ordem dentro da barra é pessimista, e latência é parâmetro, não tempo real.
* **O registo de eventos não reconstrói tudo.** Reconstrói ordens; trades e braços são tabelas gravadas directamente (ADR-005).
* **O Sharpe deflacionado depende do número de ensaios** e usa uma aproximação para correlação entre eles. A distribuição de resultados de trading tem caudas pesadas, que os intervalos normais subestimam.
* **A regra de paragem pode parar sem necessidade**, porque repete um teste a cada hora. É a direcção segura do erro.
* **Testes de stress aplicam-se à carteira do momento** e só correm com posições abertas. Reduzem risco novo, não fecham posições.
* **Em simulação, o desempenho só foi medido por barras.** Latências reais de IPC do terminal não foram medidas.
* **`bot.py` continua a ser o maior módulo**, e o `Bot` continua a ser um só objecto (ADR-017).
* **A prova de determinismo cobre o modo `baseline`.** Em modo `full`, a equipa corre em paralelo.

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
