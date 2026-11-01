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
