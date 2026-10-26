# CYBERBOX Trading Office: notas de experiência de utilização

O painel responde a três perguntas, por esta ordem de importância:

1. **Que conta é esta e está tudo bem?** (barra de topo, sempre visível)
2. **O que é que o bot está a fazer agora e porquê?** (escritório, funil)
3. **Como tem corrido?** (separadores de desempenho, risco, estratégias)

Capturas reais (modo de demonstração, Chrome) em [screenshots/](screenshots/).

## Disposição

```
+--------------------------------------------------------------------------------------+
| logo | SIMULAÇÃO/DEMO/REAL | capital | P&L hoje | almofada | exploração | modo | Claude |
|      relógios TYO LDN NYC + servidor | ⌕ Ctrl K | ? | ⚙ | ◐ | EN                    |
+--------------------------------------------------------------------------------------+
| faixa da conta (REAL a vermelho, SIMULAÇÃO a violeta)                                |
+-----------+--------------------------------------------------------------------------+
| EQUIPA 22 | linha de estado (o bloqueio principal, em português)                    |
| procurar  | separadores: Escritório · Porque não estou a negociar? · ...            |
| por depto |  +--------------------------------------------------+ +--------------+  |
|  retrato  |  | escritório (canvas): salas, personagens, ecrãs   | | fluxo de     |  |
|  nome     |  | cartão da reunião         minimapa                | | eventos      |  |
|  função   |  +--------------------------------------------------+ +--------------+  |
|  estado ◎ |                                                                          |
+-----------+--------------------------------------------------------------------------+
| ticker dos últimos eventos            | repetição 24 h: barra, reproduzir, x60, ao vivo |
+--------------------------------------------------------------------------------------+
```

* **Barra de topo:** o selo da conta é o elemento mais forte (REAL pulsa em vermelho). Os
  quatro números são os que mudam a decisão de intervir: capital, P&L de hoje, almofada acima
  do piso de lucros protegidos e quanto do orçamento de exploração de hoje já foi usado (barra
  que fica amarela aos 70% e vermelha aos 90%). O chip de modo diz Exploração, Probação,
  Activo, Em pausa ou Disjuntor. A lâmpada do Claude é verde, âmbar ou cinzenta. Os relógios
  usam códigos de cidade (TYO, LDN, NYC) e acendem quando a sessão está aberta.
* **Equipa (esquerda):** os 22 agentes por departamento, com retrato (o mesmo desenho da
  personagem), função e um ponto de estado. Clicar abre a ficha e segue a personagem no
  escritório; ◎ só segue.
* **Escritório (centro):** dez salas ligadas por dois corredores. Cada personagem é um agente
  real; só se mexe quando o bot faz algo.
* **Fluxo de eventos (direita):** frases curtas em português, filtráveis por agente, símbolo e
  gravidade. As linhas de decisão abrem o dossier.
* **Barra de baixo:** ticker dos últimos eventos e a repetição das últimas 24 horas.

## O escritório

* **Motor:** Canvas 2D, sem bibliotecas, sem passo de compilação. Fundo (chão, paredes,
  mobiliário, nomes das salas) desenhado uma vez numa imagem em memória; em cada imagem só se
  desenham os ecrãs vivos, as personagens, os efeitos e os balões.
* **Personagens:** pixel art procedural, 4 direcções, ciclo de marcha de 4 imagens, poses
  sentado, a escrever, a falar, a ler, a celebrar, veto (sinal de stop), descanso (chávena),
  bilhete (ordem) e erro. Cada agente tem aspecto estável derivado do seu id; a camisa tem a cor
  do departamento; acessórios por função (gravata da CIO, auscultadores na execução e nas
  operações, bata no laboratório, crachá no risco, boné do Guardião, cachecol da Repórter).
* **Movimento:** A* numa grelha de 60 x 40 mosaicos, 8 direcções sem cortar cantos; as
  personagens vão às suas secretárias, à mesa do conselho nas reuniões e aos sofás quando vão
  para o banco. Com movimento reduzido, mudam de lugar sem caminhar.
* **Câmara:** arrastar para mover, roda ou pinça para aproximar (até 3x), duplo clique para
  focar, setas e +/- no teclado, 0 para ver tudo; seguir uma personagem; minimapa clicável;
  "Ir para a sala..." enquadra uma sala.
* **Ecrãs vivos:** quadro de cotações a deslizar na Sala de Negociação, notícia seguinte na
  Redacção, P&L e capital na Análise, risco aberto no cofre do Risco, reunião na Sala do
  Conselho, servidores a piscar nas Operações, lâmpada do conselheiro.
* **Efeitos:** confetes nas vitórias, anel vermelho nos vetos, anel amarelo nas ordens, "!" nos
  erros, "z z" no banco; noite quando não há sessões abertas.
* **Desempenho:** monitor ligável (Preferências > Monitor de desempenho, ou Ctrl+K). No Chrome
  sem aceleração gráfica: 3 a 11 ms por imagem com os 22 agentes (orçamento de 60 fps: 16,7 ms).
  O ciclo pára quando o separador do navegador fica escondido.

## Porque não estou a negociar?

Pensado para quem abre o painel preocupado. De cima para baixo:

1. **Uma frase**: "O maior travão: X (Y% dos bloqueios, N vezes)", ou "O bot está a
   negociar", ou "Nenhuma estratégia deu sinal...".
2. A linha de estado do bot e, se houver, o bloqueio activo (piso, limite diário, pausa).
3. **Funil**: barras avaliadas, sinais, bloqueados, ordens.
4. **Bloqueios principais** com o valor de cada filtro: "protege" (os sinais bloqueados
   teriam perdido) ou "custa" (teriam ganho), e se o Doutor de Fluxo o pode ajustar.
5. **Doutor de Fluxo**: o que mudou, até onde pode ir, e a garantia de que nunca mexe no risco.
6. Orçamento de exploração (hoje e semana) e braços por nível.
7. Sinais, bloqueios e ordens por hora.
8. O que a conta consegue pagar (risco do lote mínimo por símbolo).

## Navegação e acessibilidade

* **Ctrl+K** (ou "/"): paleta de comandos com separadores, agentes (seguir ou abrir), salas,
  preferências, repetição, visita guiada e acções de controlo (se ligadas). Pesquisa sem
  acentos; correspondências exactas primeiro.
* **Teclado:** tudo é alcançável com Tab; setas nos separadores (Home/End); a lista da equipa
  é a alternativa acessível ao canvas; Esc fecha fichas, paleta, preferências e visita.
* **Leitores de ecrã:** o canvas tem uma descrição que se actualiza (agentes, ocupados,
  reunião); duas regiões vivas anunciam ordens, vetos, alertas críticos, mudanças de modo e
  perda de ligação, sem inundar (no máximo uma mensagem educada a cada 4 s).
* **Preferências** (⚙, guardadas neste navegador): tema escuro/claro, alto contraste, movimento
  (sistema, completo, reduzido), efeitos (desligados, poucos, todos), monitor de desempenho.
  O movimento reduzido segue também a preferência do sistema operativo.
* **Contraste:** texto principal `#E8EDF7` sobre `#080E1A`; o alto contraste reforça texto e
  contornos e usa amarelo para o foco.
* **Visita guiada:** 7 passos na primeira visita (selo da conta, números, equipa, escritório,
  funil, repetição, paleta); reabre com "?" ou pela paleta.
* **Telemóvel:** a equipa passa a gaveta (☰), os números deslizam na horizontal, o escritório
  ocupa 62% da altura com o fluxo por baixo, o cartão da reunião fica compacto. Sem
  deslocamento horizontal da página (verificado a 390 px).

## Estados vazios e erros

Sempre em português e sem números inventados: "Ainda não há dados: arranque o bot (ou use o
modo demo: python -m panel --demo)", "O bot não actualiza o ficheiro de saúde há N min",
"Dados insuficientes (n de m necessários)", "Nenhum braço acima de sombra ainda: o bot explora
com micro risco enquanto recolhe evidência", "O painel não consegue ler o estado do bot. A
tentar de novo...".

## Marca

Tokens, tipos de letra e logótipo em [panel/BRAND.md](../../panel/BRAND.md). Syne nos títulos
e botões (maiúsculas, espaçamento como no sítio da CYBERBOX), Space Mono no texto e nos
números, teal como cor de acção, azul-marinho nas paredes do escritório.

## Como verificar

```
python -m panel --demo
node tests\frontend_smoke.mjs http://127.0.0.1:8765/ smoke "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

O teste abre cada separador, segue um agente, abre uma ficha, usa a paleta, as preferências,
o alto contraste, a visita guiada, o tema claro e o ecrã de telemóvel, e falha com qualquer
erro de JavaScript ou deslocamento horizontal. As capturas desta pasta saíram desse teste.
Os testes unitários do motor do escritório (caminhos, planta, câmara) correm com
`node --test tests\js\office.test.mjs` e também a partir do `pytest`.
