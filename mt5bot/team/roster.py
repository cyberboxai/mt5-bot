"""The specialist team: ids, departments, rooms and powers. Pure data, no imports of the
agent implementations, so config validation and the panel can use it cheaply.

Powers, in one place:

* VOTERS cast directional votes in the CIO's bounded ensemble. Their weights are learned.
* GATES can only block or shrink an entry (data health, news, exposure, session); they do
  not vote and are not weighted.
* The Risk Manager and the Guardian are final: nothing outvotes, tilts or benches them.
* Only the CIO submits a trade request, and only for a proposal from the v2 champion.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentInfo:
    id: str
    name: str
    role_pt: str
    role_en: str
    department: str
    room: str
    icon: str
    can_decide: str
    cannot_decide: str


DEPARTMENTS = {
    "market_intelligence": ("Inteligência de Mercado", "Market Intelligence"),
    "strategy_desk": ("Mesa de Estratégia", "Strategy Desk"),
    "risk_control": ("Risco e Controlo", "Risk and Control"),
    "execution": ("Execução", "Execution"),
    "research_review": ("Investigação e Revisão", "Research and Review"),
    "operations_leadership": ("Operações e Liderança", "Operations and Leadership"),
}

ROOMS = {
    "trading_floor": ("Sala de Negociação", "Trading Floor"),
    "research_lab": ("Laboratório de Investigação", "Research Lab"),
    "risk_office": ("Gabinete de Risco", "Risk Office"),
    "execution_desk": ("Mesa de Execução", "Execution Desk"),
    "newsroom": ("Redacção", "Newsroom"),
    "ops_room": ("Sala de Operações", "Operations Room"),
    "boardroom": ("Sala do Conselho", "Boardroom"),
    "analytics": ("Canto da Análise", "Analytics Corner"),
    "advisor_booth": ("Cabine do Conselheiro Claude", "Claude Advisor Booth"),
    "break_room": ("Sala de Descanso", "Break Room"),
}

ROSTER: tuple[AgentInfo, ...] = (
    AgentInfo("data_clerk", "Dina", "Escriturária de Dados", "Data Clerk", "market_intelligence", "ops_room", "db",
              "pausar entradas quando os dados estão maus", "abrir ou fechar posições"),
    AgentInfo("regime_detector", "Rui", "Detector de Regime", "Regime Detector", "market_intelligence",
              "trading_floor", "compass", "classificar o regime de mercado", "votar ou dimensionar trades"),
    AgentInfo("session_analyst", "Sara", "Analista de Sessões e Liquidez", "Session and Liquidity Analyst",
              "market_intelligence", "newsroom", "clock", "reduzir o tamanho em horas pouco líquidas",
              "aumentar o tamanho"),
    AgentInfo("news_sentinel", "Nuno", "Sentinela de Notícias e Calendário", "News and Calendar Sentinel",
              "market_intelligence", "newsroom", "news", "bloquear entradas perto de eventos de alto impacto",
              "abrir trades ou ler instruções de notícias"),
    AgentInfo("correlation_analyst", "Carla", "Analista de Correlação e Exposição", "Correlation and Exposure Analyst",
              "market_intelligence", "risk_office", "link", "reduzir ou bloquear risco duplicado entre moedas",
              "aumentar a exposição"),
    AgentInfo("trend_analyst", "Tomás", "Analista de Tendência", "Trend Analyst", "strategy_desk", "trading_floor",
              "trend", "votar a favor ou contra (e propor, se tiver o campeão)", "dimensionar acima da base"),
    AgentInfo("mean_reversion_analyst", "Mara", "Analista de Reversão à Média", "Mean-Reversion Analyst",
              "strategy_desk", "trading_floor", "wave", "votar a favor ou contra (e propor, se tiver o campeão)",
              "dimensionar acima da base"),
    AgentInfo("breakout_analyst", "Bruno", "Analista de Rupturas e Volatilidade", "Breakout and Volatility Analyst",
              "strategy_desk", "trading_floor", "bolt", "votar a favor ou contra (e propor, se tiver o campeão)",
              "dimensionar acima da base"),
    AgentInfo("mtf_analyst", "Mateus", "Analista Multi-Período", "Multi-Timeframe Analyst", "strategy_desk",
              "trading_floor", "layers", "votar com o enviesamento do período superior", "propor trades"),
    AgentInfo("meta_labeler", "Lia", "Meta-Etiquetadora", "Meta-Labeler", "strategy_desk", "research_lab", "brain",
              "votar com a probabilidade validada fora da amostra (ou abster-se)", "propor trades"),
    AgentInfo("risk_manager", "Rita", "Gestora de Risco", "Risk Manager", "risk_control", "risk_office", "shield",
              "vetar qualquer entrada (veto final)", "ser vencida por votos, pesos ou pelo conselheiro"),
    AgentInfo("guardian", "Gil", "Guardião", "Guardian", "risk_control", "risk_office", "eye",
              "congelar entradas ou accionar o kill switch", "abrir trades ou relaxar limites"),
    AgentInfo("execution_trader", "Edu", "Operador de Execução", "Execution Trader", "execution", "execution_desk",
              "ticket", "enviar a ordem aprovada com SL/TP e verificar spread e preço", "alterar o tamanho aprovado"),
    AgentInfo("position_manager", "Paula", "Gestora de Posições", "Position Manager", "execution", "execution_desk",
              "hand", "apertar stops, fechos parciais e de tempo (só reduzem risco)", "alargar ou remover um SL"),
    AgentInfo("quant_researcher", "Quim", "Investigador Quantitativo", "Quant Researcher", "research_review",
              "research_lab", "flask", "correr a optimização walk-forward e o modo sombra", "promover sem portões"),
    AgentInfo("validation_auditor", "Vera", "Auditora de Validação", "Validation Auditor", "research_review",
              "research_lab", "check", "sinalizar sobreajuste e fugas de dados", "promover estratégias"),
    AgentInfo("performance_analyst", "Pedro", "Analista de Desempenho", "Performance Analyst", "research_review",
              "analytics", "chart", "actualizar pesos e calibração, pôr agentes no banco", "mexer no risco"),
    AgentInfo("reporter", "Rosa", "Repórter", "Reporter", "research_review", "analytics", "doc",
              "escrever relatórios e notificações", "decidir trades"),
    AgentInfo("ops_engineer", "Olavo", "Engenheiro de Operações", "Ops Engineer", "operations_leadership", "ops_room",
              "gear", "vigiar o terminal, a máquina, o painel e o conselheiro", "decidir trades"),
    AgentInfo("claude_strategist", "Ciro", "Estratega Claude (ligação ao conselheiro)", "Claude Strategist",
              "operations_leadership", "advisor_booth", "spark",
              "pedir ao conselheiro para reduzir ou vetar", "aumentar o tamanho, criar trades ou mexer em ordens"),
    AgentInfo("cio", "Isabel", "Directora de Investimentos (Orquestradora)", "Chief Investment Officer",
              "operations_leadership", "boardroom", "crown",
              "conduzir o protocolo e submeter o pedido de trade", "ultrapassar o Risco ou o Guardião"),
)

AGENT_IDS: tuple[str, ...] = tuple(a.id for a in ROSTER)
BY_ID: dict[str, AgentInfo] = {a.id: a for a in ROSTER}

VOTERS: tuple[str, ...] = ("trend_analyst", "mean_reversion_analyst", "breakout_analyst", "mtf_analyst",
                           "meta_labeler")
GATES: tuple[str, ...] = ("data_clerk", "news_sentinel", "correlation_analyst", "session_analyst")
DESK: tuple[str, ...] = ("trend_analyst", "mean_reversion_analyst", "breakout_analyst", "mtf_analyst")
NEVER_BENCHED = frozenset({"risk_manager", "guardian", "data_clerk", "ops_engineer"})
NEVER_DISABLED = frozenset({"risk_manager", "guardian", "data_clerk", "ops_engineer", "cio", "execution_trader"})
# Weight tilts from the advisor may only touch voters, never the final authorities.
TILTABLE = frozenset(VOTERS)
