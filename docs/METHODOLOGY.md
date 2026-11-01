# Metodologia de investigação, em linguagem simples

Este documento explica como o sistema decide se uma estratégia tem vantagem e o que cada número significa para si. Nenhum destes números promete lucro: todos descrevem o passado, com a incerteza à vista.

## O problema que se está a resolver

Se testar 300 estratégias sem qualquer vantagem real, algumas vão parecer óptimas por puro acaso. Escolher a melhor e confiar no resultado é o erro mais comum e mais caro da negociação algorítmica. Tudo aqui existe para o evitar.

## Custo contra vantagem

**O que é:** o quantil inferior da expectativa do braço, em dinheiro por lote, a dividir pelo custo total esperado por lote (spread, derrapagem à entrada e à saída, comissão e swap).

**Como ler:** por omissão o braço só negoceia com rácio de pelo menos 2. Um rácio de 0,5 significa que a parte provada da vantagem nem metade do custo cobre. Abaixo de zero, ainda não há vantagem provada.

**Porque importa:** no retalho, o custo mata mais estratégias do que o mercado.

## Validação cruzada purgada combinatória (CPCV)

**O que é:** a história é dividida em 6 blocos. Testa-se em cada par de blocos e treina-se nos restantes, o que dá 15 divisões e 5 percursos completos fora da amostra.

**Purga e embargo:** um trade de treino que toque num bloco de teste é retirado, e depois de cada bloco de teste há um intervalo sem treino. Sem isto, o futuro infiltra-se no treino.

**Como ler:** em vez de um único resultado histórico, obtém uma distribuição de resultados fora da amostra. É ela que alimenta a evidência inicial dos braços, com desconto.

## Registo de ensaios

**O que é:** uma contagem permanente de todas as configurações alguma vez avaliadas. A base de dados recusa apagá-la.

**Como ler:** quanto mais se testou, mais exigente fica a barra. Esquecer tentativas falhadas é a forma mais fácil de um backtest mentir.

## Sharpe deflacionado (DSR)

**O que é:** a probabilidade de o Sharpe observado ser superior ao que a melhor de N configurações sem qualquer vantagem mostraria por sorte, tendo em conta a forma da distribuição dos resultados.

**Como ler:** acima de 0,95, a sorte é uma explicação pouco provável. O limite é 0,95. Um DSR de 0,70 quer dizer que há 30% de hipótese de ser só selecção.

## Probabilidade de sobreajuste (PBO)

**O que é:** divide-se o tempo em metades de muitas formas. Para cada divisão, vê-se se a melhor configuração numa metade fica na metade de baixo da outra.

**Como ler:** 0 é o ideal. Perto de 0,5, escolher a melhor no passado não ajuda nada no futuro. O limite é 0,5.

## Registo mínimo

**O que é:** quantos trades são precisos para que o Sharpe observado seja credível. Quanto mais pequena a vantagem, mais trades são necessários.

**Como ler:** se a estratégia tem menos trades do que o mínimo, ainda não sabe o suficiente.

## Veredictos

| Veredicto | Significado | O que acontece |
|---|---|---|
| vantagem plausível | passou todas as provas acima | pode subir a T2 e T3 com evidência própria |
| sem vantagem | tem dados e falhou | braço retirado para investigação |
| insuficiente | poucos trades | fica em sombra a recolher dados |

## Evidência em níveis

Cada braço tem uma distribuição de crença sobre a sua expectativa em R. Começa na média da família, ligeiramente negativa, porque a ideia média perde depois de custos. Afasta-se só com evidência forte. Por isso o painel mostra intervalos: o traço vertical é o zero, e a barra toda à direita é o que queremos ver.

## Distância à realidade

**O que é:** para cada trade real existe um gémeo simulado do mesmo sinal. A distância é real menos simulado, em R por trade.

**Como ler:** perto de zero, a simulação merece confiança nesta conta. Muito negativa, a simulação está a enganar: as promoções congelam e os custos são recalibrados. Se o intervalo incluir zero, ainda não há diferença provada.

## Rótulos de tripla barreira

Cada trade de investigação termina como terminaria em execução: no SL, no TP ou no limite de tempo, com o stop primeiro quando a mesma barra toca nos dois. Assim, o que se mede é o que se negoceia.

## Custos calibrados

Cada ordem deixa um registo: preço de decisão, spread, preenchimento, derrapagem em pontos e em R, latência e rejeições, por símbolo, sessão e hora. Antes de haver preenchimentos, usa-se o spread histórico das barras. À medida que chegam dados, pesam mais do que a estimativa inicial. Aos 20 preenchimentos numa célula, os dados valem tanto como o ponto de partida.

## A regra de paragem

**A pergunta:** depois de todos os custos, a expectativa por trade é positiva nesta conta?

**O horizonte:** o primeiro de 200 trades reais em T2 ou acima, ou 3 meses de operação. A verificação corre a cada hora a partir de 30 trades.

| Intervalo de 95% da expectativa líquida | Veredicto | Efeito |
|---|---|---|
| todo abaixo de zero | sem vantagem detectada | a conta real passa a só investigação |
| todo acima de zero | vantagem plausível | continua, com a incerteza declarada |
| inclui zero | evidência insuficiente | continua |

Parar é automático. Voltar exige um comando seu, depois de ler o relatório. Mesmo uma "vantagem plausível" é uma estimativa do passado, não uma promessa.

## Limites destes métodos

Dados sintéticos provam que a maquinaria funciona, não que exista vantagem em mercados reais. O Sharpe deflacionado usa uma aproximação para correlação entre ensaios. Um regime de mercado novo pode invalidar tudo o que foi medido. A simulação por barras não vê o que acontece dentro de uma barra.
