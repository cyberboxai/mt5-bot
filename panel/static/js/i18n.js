// UI text. PT-PT is the default; English is a second dictionary (missing keys fall back to PT-PT).

const PT = {
  "app.title": "CYBERBOX Trading Office",
  "app.subtitle": "Escritório de negociação",
  "tab.office": "Escritório", "tab.overview": "Visão geral", "tab.performance": "Desempenho", "tab.trades": "Trades",
  "tab.team": "Equipa", "tab.strategies": "Estratégias e Investigação", "tab.risk": "Risco e Exposição",
  "tab.markets": "Mercados", "tab.advisor": "Conselheiro Claude", "tab.health": "Saúde do sistema",
  "tab.decisions": "Registo de decisões",
  "account.real": "CONTA REAL", "account.demo": "CONTA DEMO", "account.sim": "SIMULAÇÃO",
  "banner.real": "Dinheiro real: todas as ordens são reais. O painel é só de leitura por omissão.",
  "banner.demo": "Conta demo: sem dinheiro real.",
  "banner.sim": "Simulação: mercado sintético, sem corretora e sem dinheiro. Nada aqui é evidência sobre mercados reais.",
  "banner.offline": "O bot não actualiza o ficheiro de saúde há {age}. Está a correr?",
  "banner.nodata": "Ainda não há dados: arranque o bot (ou use o modo demo: python -m panel --demo).",
  "banner.paused": "Novas entradas em pausa a partir do painel.",
  "theme.toggle": "Tema", "lang.toggle": "EN",
  "clock.server": "Hora do servidor", "session.tokyo": "Tóquio", "session.london": "Londres", "session.new_york": "Nova Iorque",
  "mode.full": "equipa completa", "mode.baseline": "núcleo base (v2)", "mode.auto": "automático",
  "advisor.ONLINE": "disponível", "advisor.DEGRADED": "degradado", "advisor.OFFLINE_NO_CREDIT": "sem crédito",
  "advisor.OFFLINE_AUTH": "chave inválida", "advisor.NO_KEY": "sem chave", "advisor.DISABLED": "desligado",
  "advisor.BUDGET_CAPPED": "limite de orçamento",
  "state.idle": "parado", "state.scanning": "a observar", "state.analyzing": "a analisar", "state.debating": "a debater",
  "state.deciding": "a decidir", "state.executing": "a executar", "state.monitoring": "a vigiar",
  "state.reviewing": "a rever", "state.resting": "no banco", "state.error": "erro",
  "stance.long": "comprador", "stance.short": "vendedor", "stance.flat": "neutro", "stance.veto": "veto",
  "stance.abstain": "abstém-se", "stance.unavailable": "indisponível",
  "common.insufficient": "Dados insuficientes ({n} de {min} necessários). Sem números enganadores.",
  "common.nodata": "Sem dados ainda.", "common.table": "Tabela", "common.chart": "Gráfico", "common.csv": "CSV",
  "common.close": "Fechar", "common.loading": "A carregar...", "common.yes": "sim", "common.no": "não",
  "common.estimate": "estimativa", "common.ai": "Comentário de IA (gerado pelo conselheiro opcional; não negoceia)",
  "office.title": "O escritório ao vivo", "office.sub": "Cada personagem é um agente real; o que fazem vem do fluxo de eventos do bot.",
  "office.feed": "Fluxo de eventos", "office.agents": "Agentes (acessível por teclado)", "office.meeting": "Reunião de decisão",
  "office.replay": "Repetição (últimas 24 h)", "office.live": "Ao vivo", "office.play": "Reproduzir", "office.pause": "Pausa",
  "office.advisor_away": "ausente", "office.votes": "Votos", "office.risk": "Risco", "office.result": "Resultado",
  "office.open_dossier": "Abrir dossier", "office.filter_agent": "Agente", "office.filter_symbol": "Símbolo",
  "office.filter_severity": "Gravidade", "office.all": "todos",
  "overview.equity": "Capital (equity)", "overview.today": "P&L hoje", "overview.week": "P&L 7 dias", "overview.month": "P&L 30 dias",
  "overview.all": "P&L total", "overview.drawdown": "Drawdown face ao pico", "overview.positions": "Posições abertas",
  "overview.curve": "Curva de capital e saldo", "overview.gauges": "Indicadores de risco",
  "overview.daily_loss": "Perda diária usada", "overview.open_risk": "Risco aberto", "overview.dd_breaker": "Distância ao disjuntor de drawdown",
  "overview.champions": "Campeões", "overview.open_table": "Posições abertas (jornal)", "overview.team": "Equipa",
  "perf.live": "Trades reais", "perf.net": "Lucro líquido", "perf.win": "Taxa de acerto", "perf.avg_r": "R médio",
  "perf.exp": "Expectativa por trade", "perf.pf": "Factor de lucro", "perf.sharpe": "Sharpe", "perf.sortino": "Sortino",
  "perf.calmar": "Calmar", "perf.maxdd": "Drawdown máximo", "perf.dddays": "Duração do drawdown", "perf.best": "Melhor trade",
  "perf.worst": "Pior trade", "perf.wstreak": "Maior série de ganhos", "perf.lstreak": "Maior série de perdas",
  "perf.hist": "Distribuição dos múltiplos de R", "perf.monthly": "Retornos mensais", "perf.rolling": "Expectativa móvel (20 trades)",
  "perf.cum": "R acumulado (trades reais)", "perf.baseline": "Equipa vs núcleo base (repetição fora da amostra)",
  "perf.bh": "Comprar e manter o instrumento", "perf.shadow": "Trades sombra",
  "trades.mode": "Modo", "trades.live": "real", "trades.shadow": "sombra", "trades.status": "Estado",
  "trades.symbol": "Símbolo", "trades.open": "aberto", "trades.closed": "fechado",
  "team.leaderboard": "Classificação dos agentes", "team.weights": "Pesos ao longo do tempo (todos os regimes)",
  "team.benched": "No banco", "team.ablation": "Ablação: valor acrescentado", "team.departments": "Departamentos",
  "strat.champions": "Campeões e desafiadores", "strat.runs": "Ciclos walk-forward", "strat.candidates": "Candidatos do último ciclo",
  "strat.rejected": "Rejeitados e o portão que os rejeitou", "strat.stability": "Estabilidade dos parâmetros escolhidos por janela",
  "strat.promotions": "Promoções e despromoções", "strat.hypotheses": "Hipóteses de investigação", "strat.shadow": "Progresso em sombra",
  "risk.by_symbol": "Exposição por símbolo", "risk.by_currency": "Exposição por moeda (risco em % do capital)",
  "risk.correlation": "Correlação dos retornos", "risk.var": "VaR histórico a 1 dia (estimativa)",
  "risk.worst": "Pior cenário do risco aberto", "risk.limits": "Limites: configuração, tecto e uso actual",
  "risk.breakers": "Disjuntores", "risk.long": "longo", "risk.short": "curto",
  "markets.regime": "Regime", "markets.vol": "Percentil de volatilidade", "markets.spread": "Spread por hora do servidor",
  "markets.sessions": "Mapa de sessões (UTC)", "markets.news": "Próximas notícias de alto impacto",
  "advisor.state": "Estado", "advisor.calls": "Chamadas hoje", "advisor.cost": "Custo estimado hoje", "advisor.latency": "Latência",
  "advisor.verdicts": "Vereditos", "advisor.errors": "Erros por classe", "advisor.history": "Histórico de estados",
  "advisor.value": "Valor acrescentado medido", "advisor.briefing": "Último briefing", "advisor.research": "Investigação",
  "advisor.recent": "Chamadas recentes",
  "health.bot": "Bot", "health.alive": "a correr", "health.stale": "sem actualização", "health.series": "Métricas (24 h)",
  "health.log": "Fim do bot.log", "health.alerts": "Alertas", "health.audit": "Auditoria das acções de controlo",
  "health.restarts": "Reinícios pelo supervisor", "health.controls": "Acções de controlo",
  "control.pause": "Pausar novas entradas", "control.resume": "Retomar entradas", "control.kill": "Kill switch",
  "control.token": "Token do painel", "control.confirm": "Confirmo esta acção", "control.send": "Enviar",
  "control.disabled": "Acções de controlo desligadas: {reason}",
  "control.kill_warn": "O kill switch fecha as posições do bot e pára o bot. Use só em emergência.",
  "decisions.search": "Pesquisar", "decisions.action": "Acção", "decisions.enter": "entrada", "decisions.no_trade": "sem trade",
  "dossier.title": "Dossier de decisão", "dossier.inputs": "Entradas", "dossier.opinions": "Opiniões",
  "dossier.ensemble": "Agregação", "dossier.advisor": "Conselheiro", "dossier.risk": "Camada de risco",
  "dossier.execution": "Execução", "dossier.outcome": "Resultado", "dossier.explanation": "Explicação",
  "agent.role": "Função", "agent.can": "Pode decidir", "agent.cannot": "Não pode", "agent.task": "Tarefa actual",
  "agent.messages": "Mensagens recentes", "agent.weight": "Peso", "agent.calibration": "Calibração (Brier bruto / calibrado)",
  "agent.accuracy": "Acerto por regime", "agent.contribution": "Contribuição (ablação)", "agent.opinions": "Opiniões recentes",
  "agent.reliability": "Fiabilidade (confiança declarada vs observada)",
  "dept.market_intelligence": "Inteligência de Mercado", "dept.strategy_desk": "Mesa de Estratégia",
  "dept.risk_control": "Risco e Controlo", "dept.execution": "Execução", "dept.research_review": "Investigação e Revisão",
  "dept.operations_leadership": "Operações e Liderança",
  "regime.trend": "tendência", "regime.range": "lateral", "regime.high_vol": "volatilidade alta", "regime.illiquid": "ilíquido",
  "regime.unknown": "desconhecido",
  "verdict.confirm": "confirmar", "verdict.reduce": "reduzir", "verdict.veto": "vetar",
  "purpose.trade_review": "revisão pré-trade", "purpose.management": "gestão de posição", "purpose.probe": "sonda",
  "purpose.briefing": "briefing", "purpose.research": "investigação", "purpose.explain": "explicação",
  "promo.promote": "promoção", "promo.demote": "despromoção", "promo.rollback": "reposição manual",
  "advstatus.ok": "ok", "advstatus.unavailable": "indisponível",
  "col.param_set_id": "conjunto", "col.symbol": "símbolo", "col.trades": "trades", "col.mean_r": "R médio",
};

// Known technical reasons written by the bot core (in English, as in bot.log). Anything not
// listed is shown as written: it is diagnostic detail, not interface copy.
const REASONS_PT = [
  [/^team_mode is "(\w+)" in config$/, (m) => `team_mode = "${m[1]}" na configuração`],
  [/^auto: collecting evidence \((\d+)\/(\d+) resolved decisions\); baseline meanwhile$/,
    (m) => `automático: a recolher evidência (${m[1]}/${m[2]} decisões resolvidas); núcleo base entretanto`],
  [/^auto: (.+); using the simpler configuration$/, (m) => `automático: ${reasonText(m[1])}; usa a configuração mais simples`],
  [/^auto: (.+)$/, (m) => `automático: ${reasonText(m[1])}`],
  [/^insufficient data \((\d+)\/(\d+) resolved decisions\)$/, (m) => `dados insuficientes (${m[1]}/${m[2]} decisões resolvidas)`],
  [/^insufficient data$/, () => "dados insuficientes"],
  [/^the full team beat the baseline out of sample$/, () => "a equipa completa superou o núcleo base fora da amostra"],
  [/^the full team did NOT beat the simple baseline out of sample$/,
    () => "a equipa completa NÃO superou o núcleo base simples fora da amostra"],
  [/^team not running$/, () => "a equipa não está a correr"],
  [/^simulated advisor \(no API, no cost\)$/, () => "conselheiro simulado (sem API, sem custo)"],
  [/^simulated transient outage \(overloaded\)$/, () => "falha temporária simulada (sobrecarga)"],
  [/^simulated: credit balance too low$/, () => "simulação: saldo de crédito insuficiente"],
  [/^claude\.enabled is false$/, () => "claude.enabled está a false"],
  [/^the optional 'anthropic' package is not installed$/, () => "o pacote opcional 'anthropic' não está instalado"],
  [/^ANTHROPIC_API_KEY is not set$/, () => "a variável ANTHROPIC_API_KEY não está definida"],
  [/^key present$/, () => "chave presente"], [/^new key loaded$/, () => "nova chave carregada"],
  [/^probe succeeded$/, () => "sonda bem-sucedida"], [/^budget window reset$/, () => "janela de orçamento reiniciada"],
  [/^champion (.+) trading$/, (m) => `campeão ${m[1]} a negociar`],
  [/^external signal source$/, () => "fonte de sinais externa"],
  [/^OBSERVATION: collecting data; first optimisation (running|pending)\. No orders until a candidate is promoted\.$/,
    (m) => `OBSERVAÇÃO: a recolher dados; primeira optimização ${m[1] === "running" ? "a correr" : "pendente"}. Sem ordens até um candidato ser promovido.`],
  [/^NO TRADE: no candidate passed the gates in run (\d+); standing aside by design\. Next cycle at the weekly re-optimisation\.$/,
    (m) => `SEM TRADE: nenhum candidato passou os portões no ciclo ${m[1]}; fica de fora de propósito. Próximo ciclo na reoptimização semanal.`],
  [/^OBSERVATION: best challenger (.+): shadow (\d+)\/(\d+) trades, (\d+)\/([\d.]+) days, mean ([+-][\d.]+)R(?:, bootstrap ([\d.]+) days left)?\. No orders until a candidate is promoted\.$/,
    (m) => `OBSERVAÇÃO: melhor desafiador ${m[1]}: sombra ${m[2]}/${m[3]} trades, ${m[4]}/${m[5]} dias, média ${m[6]}R`
      + `${m[7] ? `, faltam ${m[7]} dias de arranque` : ""}. Sem ordens até um candidato ser promovido.`],
  [/^no calendar$/, () => "sem calendário"],
  [/^(\d+) events from (.+)$/, (m) => `${m[1]} eventos de ${m[2]}`],
  [/^calendar file (.+) not found$/, (m) => `ficheiro de calendário ${m[1]} não encontrado`],
  [/^advisor switched off with --no-claude$/, () => "conselheiro desligado com --no-claude"],
];

const EN = {
  "app.subtitle": "Trading office",
  "tab.office": "Office", "tab.overview": "Overview", "tab.performance": "Performance", "tab.trades": "Trades",
  "tab.team": "Team", "tab.strategies": "Strategies & Research", "tab.risk": "Risk & Exposure", "tab.markets": "Markets",
  "tab.advisor": "Claude Advisor", "tab.health": "System Health", "tab.decisions": "Decision Log",
  "account.real": "REAL ACCOUNT", "account.demo": "DEMO ACCOUNT", "account.sim": "SIMULATION",
  "banner.real": "Real money: every order is real. The panel is read-only by default.",
  "banner.demo": "Demo account: no real money.",
  "banner.sim": "Simulation: synthetic market, no broker, no money. Nothing here is evidence about real markets.",
  "banner.offline": "The bot has not updated its health file for {age}. Is it running?",
  "banner.nodata": "No data yet: start the bot (or use demo mode: python -m panel --demo).",
  "banner.paused": "New entries paused from the panel.",
  "theme.toggle": "Theme", "lang.toggle": "PT", "clock.server": "Server time",
  "session.tokyo": "Tokyo", "session.london": "London", "session.new_york": "New York",
  "mode.full": "full team", "mode.baseline": "baseline core (v2)", "mode.auto": "automatic",
  "common.insufficient": "Insufficient data ({n} of {min} needed). No misleading numbers.",
  "common.nodata": "No data yet.", "common.table": "Table", "common.chart": "Chart", "common.close": "Close",
  "common.loading": "Loading...", "common.estimate": "estimate",
  "common.ai": "AI commentary (generated by the optional advisor; it never trades)",
  "office.title": "The live office", "office.sub": "Each character is a real agent; what they do comes from the bot's event stream.",
  "office.feed": "Event feed", "office.agents": "Agents (keyboard accessible)", "office.meeting": "Decision meeting",
  "office.replay": "Replay (last 24 h)", "office.live": "Live", "office.play": "Play", "office.pause": "Pause",
  "office.advisor_away": "away", "office.open_dossier": "Open dossier",
  "overview.equity": "Equity", "overview.today": "P&L today", "overview.curve": "Equity and balance",
  "overview.gauges": "Risk gauges", "control.pause": "Pause new entries", "control.resume": "Resume entries",
  "control.kill": "Kill switch", "control.token": "Panel token", "control.confirm": "I confirm this action",
  "control.send": "Send",
};

let current = "pt-PT";

export function setLanguage(lang) { current = lang === "en" ? "en" : "pt-PT"; document.documentElement.lang = current; }
export function language() { return current; }

export function t(key, vars = {}) {
  const dict = current === "en" ? EN : PT;
  let text = dict[key] ?? PT[key] ?? key;
  for (const [k, v] of Object.entries(vars)) text = text.replace(`{${k}}`, String(v));
  return text;
}

// Label for an enumerated value (department, regime, verdict...); unknown values pass through.
export function tv(prefix, value) {
  if (value === null || value === undefined || value === "") return "";
  const key = `${prefix}.${value}`;
  return current === "en" ? (EN[key] ?? String(value)) : (PT[key] ?? String(value));
}

export function reasonText(text) {
  if (!text || current === "en") return text || "";
  for (const [re, fn] of REASONS_PT) {
    const m = String(text).match(re);
    if (m) return fn(m);
  }
  return String(text);
}
