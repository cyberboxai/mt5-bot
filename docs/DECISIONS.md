# Registo de decisões (v4)

Formato ADR: contexto, decisão, consequências. Cada decisão tem um número estável. "Pressuposto" marca o que não foi possível confirmar nesta máquina (Linux, sem terminal MT5, sem chave da API, sem histórico real).

## ADR-001: O kernel é o `Bot` com portas, não uma reescrita

**Contexto.** A tese exige um só kernel em backtest, replay, sombra e real. O `Bot` já era o único caminho de decisão e tinha 980 testes à volta. Reescrevê-lo de raiz criaria o risco que a tese quer eliminar: duas versões a divergir.

**Decisão.** O `Bot` passa a depender só de `BrokerPort` e `MarketDataPort`. Os cerca de 95 pontos que tocavam o terminal passaram pelas portas. `Mt5Adapter` é o único módulo que importa `MetaTrader5`. Real, sombra, simulação e replay diferem apenas no adaptador.

**Consequências.** Os testes antigos passaram a exercitar o adaptador real, com `FakeMT5` por trás. `tests/test_kernel.py` prova que o caminho simulado e o caminho real geram ordens e registos idênticos. O determinismo está provado no modo `baseline`. Em modo `full`, os agentes correm em paralelo e essa prova não foi feita.

## ADR-002: Investigação rápida fica fora do kernel, com primitivas partilhadas

**Contexto.** O optimizador avalia centenas de configurações. Passar cada uma pelo `Bot` inteiro seria ordens de grandeza mais lento.

**Decisão.** `backtest.simulate` mantém-se como caminho vectorizado para investigação. Partilha `plan_trade`, `exit_for_bar` e os indicadores em lote com o caminho real. Os livros sombra usam as mesmas primitivas barra a barra.

**Consequências.** "Um kernel" é literal para decisões, risco e ordens, e não para a simulação de milhares de ensaios. A distância à realidade é a salvaguarda contínua contra a divergência entre os dois.

## ADR-003: Uma ordem incerta nunca é reenviada

**Contexto.** A v3.1 tratava `None`, 10012 e 10031 como "não executada". Se o corretor tivesse preenchido, a posição ficava fora do diário e um reenvio podia duplicá-la.

**Decisão.** Sem prova do resultado, a ordem fica `UNCERTAIN`. Reconcilia-se por posições, ordens e negócios. Uma entrada nunca é reenviada, nem depois de provado que não executou, porque o sinal já envelheceu. A prova de não execução exige que o corretor responda e nada tenha com aquele id durante o período de carência, medido em relógio monotónico. Enquanto houver ordem por resolver, o símbolo não aceita entradas.

**Consequências.** Perde-se alguma entrada legítima em troca de nunca duplicar. Um salto do relógio do PC não prova nada. O fecho é tratado à parte: repete-se, mas relendo a posição antes de cada tentativa, porque em netting um fecho repetido abriria posição contrária.

## ADR-004: Id de cliente determinístico, derivado da decisão

**Decisão.** O id é um hash de magic, símbolo, período, barra, braço, estratégia, lado e parte, enviado no comentário como `cb4-<12 hex>` (16 caracteres). A mesma decisão gera sempre o mesmo id, mesmo depois de reiniciar.

**Consequências.** Um arranque a meio de uma barra não consegue duplicar. Pressuposto: o corretor preserva o comentário na entrada. Se não preservar, a reconciliação adopta uma posição do mesmo símbolo, lado e magic que nada explique, em vez de ignorar a discrepância.

## ADR-005: Registo de eventos com imutabilidade na base de dados

**Decisão.** `ledger` é só de acrescento: gatilhos SQL rejeitam UPDATE e DELETE. Eventos de ordem são gravados com `synchronous=FULL` antes do envio. `orders` é uma projecção reconstruível (`project_orders`), verificada em teste.

**Consequências e limite.** O registo é a fonte de verdade para ordens, decisões, níveis, configuração e adaptações. `trades`, `arms` e `funnel_events` continuam a ser tabelas de leitura gravadas directamente, não reconstruídas do registo. Reescrevê-las como projecções puras tocaria quase todo o sistema sem ganho para o proprietário, por isso ficou de fora. O `ledger` não tem retenção: cresce por decisão, e cresce pouco.

## ADR-006: Porta de custo sobre a expectativa líquida

**Contexto.** A posterior em R já é líquida de custos, porque os livros sombra pagam spread, comissão e derrapagem. Exigir que o quantil inferior líquido cubra k vezes o custo total é mais severo do que comparar vantagem bruta com custo.

**Decisão.** Aplicou-se a leitura literal e mais conservadora, com k = 2 e limites 1 a 10.

**Consequências.** Com a omissão (quantil de 5%, `costs.edge_z`), um braço com P(E>0) de 0,81 ainda tem quantil inferior negativo, logo T2 e T3 só negoceiam com evidência muito forte. No teste de ciclo de vida, com vantagem injectada, um braço promovido a T2 ficou bloqueado por esta porta. Isto é a leitura estrita do pedido, e é uma decisão a rever pelo proprietário quando houver dados reais. Em conta real, nenhum braço sem quantil inferior positivo negoceia. A exploração T1 fica bloqueada em real por omissão e isenta em demo, porque é em demo que se medem preenchimentos próprios (`costs.exploration_exempt_demo`). Espera-se pouca actividade em real no início. É intencional.

## ADR-007: Indicadores O(1) em série só de acrescento

**Contexto.** Pedia-se actualização O(1) por barra e igualdade com o lote. Reescrever 15 estratégias de forma incremental arriscava divergir delas próprias.

**Decisão.** O ciclo mantém um `SeriesBuffer` por símbolo e período. Os indicadores de uma vista desse buffer prolongam o último resultado com as classes de `core/incremental.py`. Qualquer outra entrada usa o caminho em lote. As estratégias não mudaram.

**Consequências.** `tests/test_incremental.py` prova igualdade para cada indicador e para os sinais de cada estratégia. As operações vectoriais dentro de `compute` continuam a percorrer a janela: cada indicador é O(1), mas a avaliação de um braço é O(n) numpy, com n limitado a duas vezes `bars`. Compactar a série cria nova semente nas EMAs, sem efeito prático após a convergência.

## ADR-008: Aconselhamento por trade começa em sombra

**Contexto.** Para provar valor por ablação são precisas revisões; para as usar é preciso valor provado.

**Decisão.** `claude.review_trades` é falso por omissão. Se o proprietário o ligar, as revisões ficam registadas sem alterar nada até `advisor_gating_earned` confirmar amostras suficientes, Sharpe adicional positivo e valor acima do gasto. `review_requires_ablation: false` é a escolha explícita de saltar essa prova.

## ADR-009: Relatórios do conselheiro por Message Batches

**Decisão.** Relatórios não urgentes usam `client.messages.batches` no modelo pequeno (`claude-haiku-5-5`), a metade do preço, com prefixo de sistema em cache. O formato foi confirmado na referência do SDK, não com chamadas reais. Há limite mensal além do diário.

**Consequências.** Um relatório pode demorar até 24 horas. Pressuposto: não testado contra a API real, porque não há chave nesta máquina.

## ADR-010: Doutor de Fluxo exige prova, não apenas ausência de dano

**Contexto.** A v3.1 só bloqueava um alívio quando os sinais bloqueados perdiam comprovadamente. Sem dados, aliviava.

**Decisão.** Um alívio exige pelo menos 10 sinais bloqueados com repetição contrafactual e média não negativa, com no máximo 3 alívios por semana. Reverter e apertar nunca têm limite.

**Consequências.** A repetição contrafactual usa os gémeos em sombra dos sinais bloqueados. Não mede o efeito de segunda ordem de mudar o filtro.

## ADR-011: Sete serviços como mapa, não como reescrita

**Decisão.** `services.py` mapeia os 22 agentes para Dados, Sinais, Alocação, Risco, Execução, Investigação e Operações. O painel mostra actividade real por serviço a partir do funil. O Gestor de Risco e o Guardião continuam finais.

**Consequências.** O número de agentes não baixou: baixou o que corre em decisão por omissão. A equipa completa só decide se a ablação provar valor (`team_mode: auto`). Remover agentes sem dados seria um palpite.

## ADR-012: Regra de paragem sobre trades T2 ou acima

**Decisão.** O veredicto usa só trades reais em T2 ou acima, líquidos de custos, com intervalo normal de 95%. Verifica-se a cada hora desde 30 trades. Limite superior abaixo de zero passa a conta real a só investigação, sem intervenção humana. Voltar exige `--reenable-real`.

**Consequências.** Micro-trades de exploração não condenam nem salvam o sistema. O intervalo normal é uma aproximação com caudas pesadas: com poucos trades pode ser optimista. A verificação contínua repete um teste e aumenta os falsos positivos. Aqui o erro é parar de negociar sem necessidade, a direcção segura. O proprietário decide se reactiva.

## ADR-013: Horas de negociação limitadas pelo relógio do corretor

**Contexto.** O teste a um domingo revelou que `_weekend_flatten` usava o relógio do PC e fechava posições mesmo com o feed a dizer terça-feira.

**Decisão.** Regras de calendário usam sempre o tempo do último tick. Só sem ticks recorrem ao relógio.

## ADR-014: Registo de posições desconhecidas em vez de congelamento

**Contexto.** Antes, uma posição fora do diário só provocava congelamento pelo Guardião, e só o arranque a registava.

**Decisão.** A reconciliação periódica adopta-a, calcula o risco por inteiro, regista `reconcile.adopted` e alerta. Se houver ordem por resolver, espera, porque essa ordem pode explicar a posição.

## ADR-015: Universo por omissão em M15, H1, H4 e D1

**Decisão.** M5 saiu da omissão: é elegível, mas só se passar a porta de custo. H4 e D1 entraram por terem melhor relação entre vantagem e custo para spreads de retalho. Isto é hipótese, não medição, porque não há dados reais.

## ADR-016: Um só motor de preenchimento

**Decisão.** `sim/terminal.py` foi removido. `SimExchange` é o único motor de preenchimento, com três fachadas: `SimBroker`, `ShadowBroker` e `FakeBroker`. Os seus números de sistema (constantes) são verificados contra o pacote real no arranque.

**Consequências.** `SimBroker` paga spread, derrapagem, comissão, swap e stop-out, com rejeições e preenchimentos parciais configuráveis. Latência é parâmetro do modelo e não atrasa o relógio virtual.

## ADR-017: Divisão do `Bot` por ficheiros, com composição por herança

**Contexto.** Com a v4, `bot.py` chegou a 2659 linhas. A lógica nova de cauda, governo e operações tinha caído no mesmo sítio.

**Decisão.** Os métodos foram movidos, sem alteração, para `bot_orders.py`, `bot_gates.py` e `bot_oversight.py`, como classes que o `Bot` herda. Ficou com 1974 linhas, menos do que na v3.1.

**Consequências.** Os ficheiros são coesos e a mudança foi mecânica, com os testes a provar que nada mudou. Mas continua a ser um só objecto com muito estado partilhado: isto organiza o código, não separa responsabilidades como faria uma composição real. Essa é a dívida mais óbvia que fica na arquitectura. Fazê-la exigiria mudar centenas de referências e foi adiada de propósito.

## ADR-018: Interface em duas fases para `claude.review_trades`

**Decisão.** `review_trades` controla se há revisão. `review_requires_ablation` controla se a revisão pode alterar uma ordem. A simulação de demonstração desliga a segunda, porque o conselheiro simulado não tem custo a justificar.

## ADR-019: Tecto de exposição próprio para o lote mínimo

**Contexto.** O diagnóstico mostrou um segundo bloqueio atrás do primeiro. `approve_entry` recusa exposição nocional acima de `max_symbol_exposure_x` (5, tecto 10). 0,01 lote de EURUSD tem cerca de 1 160 USD de nocional, ou seja 17,8x num capital de 65 USD. Corrigir só o lote mínimo deixaria o bot sem trades.

**Decisão.** Um trade exactamente no lote mínimo usa `limits.MAX_MIN_LOT_EXPOSURE_X = 30`. Acima do lote mínimo mantém-se o limite normal, e o plano encolhe até caber em vez de ser recusado, para que uma conta maior nunca fique bloqueada onde uma menor negoceia. A secção 3 da especificação não lista este limite entre os invariantes, mas ele estava em `limits.py`, por isso a decisão fica aqui.

**Consequências.** O risco ao SL continua limitado a 2 % e a guarda de margem mantém-se. O que aumenta é a exposição a um gap: com 18x, um salto de 1 % contra a posição custa 18 % do capital se atravessar o stop. 30x é uma razão fixa, igual à alavancagem máxima europeia para majors a retalho, não um saldo. O piso de viabilidade inclui este tecto.

## ADR-020: Orçamento medido em trades, e T1 acima do antigo tecto

**Decisão.** `orçamento = max(pct x capital, n trades x risco de um trade)`, limitado pela perda diária e pelo limite semanal. O risco de um trade é o maior entre a mediana do lote mínimo dos candidatos viáveis e o risco T1. `MAX_EXPLORATION_TRADE_PCT` passou de 0,25 para 0,5, para o T1 de 0,5 % pedido em `aggressive`. Os tectos percentuais do orçamento mantêm-se.

**Consequências.** Em `aggressive` o orçamento de exploração passa a valer perto de 2 % por dia numa conta grande. A exploração deixa de ser barata: é o preço pedido pelo proprietário.

## ADR-021: Conta real por omissão no ficheiro, nunca no código

**Decisão.** `config.json` traz `account_mode: "real"` e `real_money_confirmed: true`, como pedido. O valor por omissão do código para `real_money_confirmed` continua `false`: um ficheiro que nunca fala de dinheiro real recusa. Uma migração nunca liga a confirmação.

**Consequências.** O repositório é público: quem o clonar recebe uma configuração que negoceia dinheiro real se tiver uma conta real no terminal. Isto foi decisão explícita do proprietário.

## ADR-022: Portas de entrada e de escala separadas

**Decisão.** T1 é onde vivem as portas de entrada: média depois de custos acima de zero, `P(E>0) >= 0,55`, custo sobre vantagem de pelo menos 1,2 sobre a média, 30 trades fora da amostra. Acima disso aplica-se a porta de escala: limite inferior com `k = 2`, veredicto de investigação e distância à realidade. Se a porta de escala falhar mas a de entrada passar, o trade faz-se no tamanho de entrada. A exploração em conta real deixou de passar pela razão custo sobre vantagem, porque o limite inferior era sempre negativo; passa a ser limitada pelo limiar da média e pelo orçamento.

## ADR-023: O valor esperado parte da posterior

**Contexto.** A fórmula pedida é `EV = P(ganho) x ganho médio - P(perda) x perda média - custo`. As médias observadas têm custos incluídos e são ruidosas.

**Decisão.** A probabilidade e as médias partem da média posterior e da geometria do trade; os resultados do braço só as movem tanto quanto a posterior confia neles (mesmo factor de encolhimento). `custo` é só o custo acima do habitual, por exemplo um spread mais largo do que o normal nesta hora. Sem dados, EV é igual à média posterior.

**Consequências.** Um braço com sorte não ganha prioridade. A meta-etiquetadora continua no estágio da equipa, porque corre depois da alocação; ainda não entra no EV.

## ADR-024: Política de stop como parte do braço, sem mudar a base de dados

**Decisão.** Os múltiplos ATR são conjuntos de parâmetros (`sl_mult`) do braço, dentro da grelha de cada estratégia. O stop de estrutura é uma estratégia que envolve outra (`structstop_*`), que muda só a distância e fica na família da base, para o encolhimento. Registados só para `ema_cross` e `donchian`, para não inflar o número de ensaios.

**Consequências.** Mais braços: `universe.max_arms` passou de 400 para 1400 (6 símbolos, 4 períodos, 56 braços). Os braços que só diferem no stop partilham o cálculo das entradas: cerca de 68 ms por par símbolo e período em Linux. Medir em hardware real faz parte da verificação em Windows.

## ADR-025: Detector de deriva com alarmes raros

**Decisão.** Page-Hinkley com `delta = 0,15` e `lambda = 15`. Para `sigma` de cerca de 1,2R isto dá perto de 600 trades entre falsos alarmes e detecta uma queda de 0,5R em cerca de 43 trades. A primeira escolha (`0,05` e `8`) daria alarmes falsos em menos de 200 trades, e cada alarme despromove um braço verdadeiro. Só uma queda dispara. Ao reiniciar, o histórico reconstrói o detector sem repetir alarmes antigos.

## ADR-026: Actualização online na passagem seguinte, não em cada trade

**Decisão.** Fechar um trade marca a evidência como desactualizada e as posteriores recalculam-se na passagem de manutenção seguinte, no máximo um minuto depois. Calcular a cada fecho dentro do ciclo de negociação custaria demasiado com centenas de braços.

## ADR-027: O dia gravado não foi repetido

**Contexto.** A especificação pede a repetição do dia real do proprietário. Esses dados não existem nesta máquina.

**Decisão.** Os testes usam uma sessão simulada, identificada como tal, com as especificações e os stops inferidos. `--replay-day` corre o mesmo código sobre as velas em cache na máquina Windows. Inventar um resultado teria sido pior do que não o ter.

## ADR-028: Ordens filhas perdem-se num reinício

**Decisão.** Uma entrada acima da capacidade envia a primeira ordem de imediato e as restantes a cada `capacity.child_interval_seconds`. Cada uma tem a sua chave e o seu SL/TP. A fila vive em memória: depois de um reinício a entrada fica menor, o lado seguro. Se o bot parar de poder entrar, ou a posição já não existir, as restantes são abandonadas: nunca se persegue uma entrada.

## Lista de valor dos componentes (orçamento de complexidade)

Janela de avaliação: 90 dias de operação real. Sem valor demonstrado até lá, o componente fica desligado por omissão e documentado aqui. Hoje não há dados reais, por isso nenhuma linha afirma valor.

| Componente | Como se mede | Estado por omissão | Valor conhecido hoje |
|---|---|---|---|
| Votos da equipa | ablação contra o núcleo base, bootstrap emparelhado | desligado até provar (`auto`) | nenhum em dados reais |
| Meta-etiquetadora | Sharpe marginal por ablação | ligado, abstém-se sem dados validados | nenhum em dados reais |
| Doutor de Fluxo | repetição contrafactual de cada alívio | ligado, limitado a 3 por semana | nenhum em dados reais |
| Conselheiro por trade | valor adicional depois do custo | desligado | nenhum |
| Políticas de gestão | repetições emparelhadas por braço | ligado, fixo até haver evidência | nenhum em dados reais |
| Encaminhamento por regime | evidência do braço por regime | ligado, só bloqueia regimes claramente negativos | nenhum em dados reais |
| Pesquisa de oportunidades | selecção de universo contra universo fixo | ligado | ainda sem ablação |

`python mt5_bot.py --complexity-report` mostra este estado com os dados da conta.
