# Estender o bot: estratégias, políticas, agentes e painel

Tudo o que o bot executa é código revisto e testado que vem com o projecto. Nada é gerado,
avaliado ou descarregado em tempo de execução.

## 1. Uma estratégia nova é um ficheiro

1. Crie `mt5bot\strategies\a_minha.py`:

```python
"""Exemplo: rompimento de N velas confirmado pela EMA. Sem vantagem comprovada."""

import numpy as np

from .. import indicators
from .base import Param, Params, Strategy, crosses_above, crosses_below, sides_from
from .registry import register


@register
class AMinha(Strategy):
    name = "a_minha"
    description = "Rompimento de N velas na direcção da EMA longa."
    param_space = (
        Param.int_range("lookback", 20, 60, 10),
        Param.int_range("trend_ema", 100, 200, 50),
        Param.float_range("sl_mult", 1.5, 3.0, 0.5),
        Param.float_range("tp_mult", 2.0, 4.0, 0.5),
    )

    def warmup_bars(self, params: Params) -> int:
        return 3 * int(params["trend_ema"]) + 2

    def compute(self, bars: np.ndarray, params: Params) -> np.ndarray:
        close = np.asarray(bars["close"], dtype=float)
        trend = indicators.ema(close, int(params["trend_ema"]))
        upper = indicators.rolling_max_prev(bars["high"], int(params["lookback"]))
        lower = indicators.rolling_min_prev(bars["low"], int(params["lookback"]))
        with np.errstate(invalid="ignore"):
            buy = crosses_above(close, upper) & (close > trend)
            sell = crosses_below(close, lower) & (close < trend)
        return sides_from(buy, sell)
```

2. Regras do contrato: `compute` recebe **só velas fechadas** e devolve, para cada vela, `+1`
   (compra), `-1` (venda) ou `0`; o valor da vela `i` só pode usar dados até `i`; nada de rede,
   ficheiros ou ordens. SL e TP são `ATR(14) x sl_mult` e `ATR(14) x tp_mult`. Volume, filtros e
   limites são da camada de risco. Para indicadores pesados use `indicators.cached(bars,
   "nome", lambda: ...)`: o mesmo cálculo é partilhado por todos os braços dessa série.
3. Diga a que analista pertence a família, em `mt5bot\team\agents\desk.py`, função
   `family_for` (tendência, rupturas ou reversão à média). Sem isso fica na mesa de tendência.
4. Corra os testes de contrato (determinismo, sinais nos dados de teste, ausência de
   lookahead, limites de parâmetros), que passam a correr sozinhos para a nova estratégia:
   ```
   pytest -q tests\test_strategies.py
   ```
5. No arranque seguinte o bot cria um braço T0 por símbolo e período (se
   `universe.default_arms` estiver ligado e couber em `universe.max_arms`), e a investigação
   inclui-a no ciclo seguinte. Começa em sombra: só sobe com evidência.

**Limites de parâmetros:** poucos valores por parâmetro, com significado; `sl_mult` e
`tp_mult` dentro dos limites de `mt5bot\limits.py` (0,5 a 6 e 0,5 a 10). Mais valores =
mais combinações, mais risco de sobre-optimização.

## 2. Uma política de gestão nova

As políticas estão em `mt5bot\management.py`, no dicionário `RULES`
(`PolicyRules(breakeven_after_r, trail_after_r, trail_atr_mult, time_stop_bars)`).

1. Acrescente uma entrada com um nome novo e o seu texto em `POLICY_PT`.
2. A repetição emparelhada (`replay`) passa a calculá-la para cada trade em sombra; a escolha
   por braço (`choose`) considera-a sozinha.
3. Regras: só pode apertar o stop ou fechar mais cedo. Escreva testes em
   `tests\test_management.py` (caminho conhecido, nunca sai depois da saída fixa, nunca perde
   mais).

## 3. Um agente novo

Um agente é uma classe revista e testada. O caminho mais simples é um **agente de contexto**
que pode reduzir o tamanho ou bloquear; nunca aumentar.

1. Registe-o no elenco, em `mt5bot\team\roster.py`, acrescentando um `AgentInfo` à lista
   `ROSTER` (id, nome, função em português e inglês, departamento, sala, ícone, o que pode e o
   que não pode decidir). Para participar na reunião como contexto, acrescente o id a `GATES`
   em `roster.py` e a `CONTEXT_STAGE` em `mt5bot\team\agents\cio.py`.
2. Crie a classe (por exemplo em `mt5bot\team\agents\market.py`) com o método
   `analyze(self, ctx) -> Opinion`. `size_hint` entre 0 e 1 (só reduz); `stance="veto"`
   bloqueia. O tempo limite é `team.agent_timeout_ms`; se falhar, é isolado e a reunião
   continua sem ele.
3. Acrescente a classe a `AGENT_CLASSES` em `mt5bot\team\agents\__init__.py`, na mesma
   posição que no `ROSTER`.
4. Testes: o caso normal, o caso que reduz e que uma falha do agente não muda a decisão para
   pior. Actualize os testes que contam 22 agentes e a lista do escritório em
   `tests\js\office.test.mjs` (o teste `tests\test_panel_ui.py` compara-a com o `ROSTER`).
5. O escritório dá-lhe secretária, personagem e cores do departamento sozinho. As frases que
   diz (`message`) aparecem nos balões: escreva-as em português.

## 4. Um elemento novo no painel

1. **Dados:** acrescente o valor ao método do separador em `panel\data.py`. As bases abrem-se
   só para leitura; devolva `null` ou uma lista vazia quando não houver dados (nunca invente
   números).
2. **Teste:** em `tests\test_panel.py`, verifique o novo campo e que nada de proibido sai.
3. **Visual:** em `panel\static\js\views\`, use `chartCard`, `tile`, `card` ou `dataTable`.
   Regras: texto com `textContent` (nunca `innerHTML`); nenhum script ou estilo inline (a
   política de segurança bloqueia-os); cores só com variáveis de `tokens.css`; textos em
   `i18n.js` em português (o teste `test_every_literal_string_key_exists_in_portuguese`
   falha se faltar algum).
4. Verifique com `start_panel.bat --demo` e, se tiver Node.js e Chrome, com
   `node tests\frontend_smoke.mjs` (ver [ux/UX_NOTES.md](ux/UX_NOTES.md)).
