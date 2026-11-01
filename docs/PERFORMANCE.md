# Desempenho: antes e depois

Medições de `tools/benchmark.py` na mesma máquina (Linux, 4 CPU, Python 3.12), com dados sintéticos: 300 barras M5, 5 símbolos, 3 períodos e 3000 barras de aquecimento. Foram duas corridas alternadas de cada versão, com a máquina em repouso, e a tabela mostra a média. v3.1 é o commit `821e3b7`, numa árvore de trabalho separada. A v4 corre 2 estratégias a mais e mais verificações por decisão.

A primeira comparação foi deitada fora: um processo da simulação tinha ficado órfão e a consumir CPU. Esse defeito está corrigido e tem teste de regressão.

## Resultados

| Métrica | v3.1 | v4 | Diferença |
|---|---:|---:|---:|
| Ciclo com barra nova, p50 | 271 ms | 218 ms | -20% |
| Ciclo com barra nova, p95 | 573 ms | 487 ms | -15% |
| Ciclo com barra nova, p99 | 753 ms | 752 ms | sem melhoria real |
| Máximo | 1159 ms | 1316 ms | pior 14% |
| CPU da corrida | 106,5 s | 84,1 s | -21% |
| Tempo total | 105,2 s | 86,4 s | -18% |
| Ciclo de optimização | 4,6 s | 4,2 s | -9% |
| Chamadas de barras por ciclo sem barra nova | 15,0 | 0,0 | -100% |
| Alterações de linhas SQLite por barra | 352 | 272 | -23% |
| Memória máxima (RSS) | 66 MiB | 74 MiB | pior 12% |
| Crescimento da base de dados por barra | 18,1 KiB | 18,2 KiB | sem melhoria |

Os valores de ciclo vêm de `Bot.run_once`, sem o gerador de barras da simulação. A corrida usa 15 pares de símbolo e período.

## O que explica os ganhos

- **Sem chamadas em ciclos vazios.** O relógio do corretor indica quando uma barra pode ter fechado, e até lá o ciclo não pede barras. Com ciclos de 5 segundos, quase todos são vazios. Num terminal real cada chamada atravessa IPC, por isso aqui deve estar o maior ganho real. Em simulação o tempo de um ciclo vazio não muda (0,7 ms), porque a chamada simulada é barata. O ganho em tempo real não foi medido sem terminal.
- **Indicadores O(1).** A série só cresce, e EMA, ATR, RSI, ADX, Donchian, Bollinger, percentil e mediana prolongam o último resultado. A igualdade com o cálculo em lote está provada em teste, incluindo os sinais de todas as estratégias.
- **Só barras novas.** Depois da primeira leitura, o ciclo pede 3 barras e não `bars + 1`.
- **Menos escritas.** O estado de cada livro sombra só é gravado quando muda. Um teste prova que uma falha entre gravações repete para exactamente os mesmos trades.
- **Lotes.** Eventos da equipa agrupam-se em janelas de 0,25 s, e os ticks gravam-se em blocos.
- **Saúde compacta.** `health.json` usa o codificador em C, sem indentação.

## O que não melhorou, e porquê

- **p99 e máximo.** A cauda é dominada por limpeza periódica e por ciclos com vários braços a abrir e fechar trades simulados. A v4 ainda acrescenta o livro de ordens, a escrita antecipada e as verificações pós-preenchimento, que são pagas onde importa.
- **Tamanho da base de dados.** O `ledger`, a TCA e as ordens acrescentam dados que antes não existiam, e isso anula a poupança. É uma troca intencional: auditabilidade em vez de bytes. As retenções de 30 dias do funil e dos ticks continuam.
- **Memória.** Os buffers de série reservam duas vezes `bars` por par, e as saídas dos indicadores ficam guardadas.
- **Avaliação de um braço.** Os indicadores são O(1), mas as operações vectoriais dentro de `compute` continuam a percorrer a janela (ADR-007).

Estes números vêm de uma simulação em que a limpeza corre em todas as barras. Em operação real ela corre a cada 60 segundos.

## Orçamento por ciclo

`ops.cycle_budget_ms` (2000 ms por omissão) mede cada ciclo. Acima do orçamento, regista os tempos por etapa em `stages_ms`, no `health.json`, e salta as etapas opcionais (pesquisa de oportunidades e Doutor de Fluxo) até o ciclo recuperar. Risco, execução e reconciliação nunca são saltados.

## Reproduzir

```cmd
.venv\Scripts\python tools\benchmark.py --out resultado.json --bars 300
```

Pare o painel e qualquer simulação antes de medir. `docs/bench_v31.json` guarda a primeira medição da v3.1, feita antes de qualquer alteração ao código.
