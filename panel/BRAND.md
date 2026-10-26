# CYBERBOX no painel: marca e tokens

Este ficheiro diz de onde vem a identidade visual do painel e como a usar. O painel não
inventa cores: tudo vem de `panel/static/css/tokens.css`, que copia a camada de marca do
sítio da CYBERBOX e define por cima dela uma camada semântica.

## Fontes da marca (verificadas)

| O quê | Origem |
|---|---|
| Cores, tipos de letra, botões | `Dev/cyberbox_website/public/index.html`, bloco `:root` (variáveis `--teal`, `--blue`, `--navy`, `--red`, `--orange`, `--yellow`, `--cyan`, `--dark`, `--darker`, `--glass`, `--glassb`, `--text`, `--muted`, `--fh`, `--fm`) |
| Variante de ferramenta interna (densidade, superfícies em camadas) | `Dev/cyberbox_acf/templates/base.html`, `tailwind.config` (cores `cb.*`, JetBrains Mono e Space Grotesk). Usada só como referência de densidade; as cores do painel seguem o sítio público. |
| Logótipo oficial | `Documents/Wilker_Backup/Pictures/CYBERBOX/cyberbox_logo.png` (1744 x 1760, enviado pelo cliente). É o mesmo ficheiro que o sítio incorpora em base64 (SHA-256 idêntico). |

Os ficheiros `.env` desses projectos não foram lidos.

## Camada de marca (`--cbx-*`)

| Token | Valor | Uso |
|---|---|---|
| `--cbx-teal` | `#00C9A7` | cor de acção e de "bom"; botão principal, nível activo |
| `--cbx-blue` | `#1874CD` | conta DEMO, série de gráficos, ecrãs dos monitores |
| `--cbx-navy` | `#1A3A6E` | paredes do escritório, departamento de Operações e Liderança |
| `--cbx-red` | `#E63030` | conta REAL, Risco e Controlo; nunca decorativo |
| `--cbx-orange` | `#F47C20` | Execução, avisos sérios |
| `--cbx-yellow` | `#FFE600` | avisos, gravata da CIO, foco em alto contraste |
| `--cbx-cyan` | `#00CFCF` | Inteligência de Mercado, foco do teclado |
| `--cbx-dark` / `--cbx-darker` | `#080E1A` / `#050A10` | fundo da página e das barras |
| `--cbx-text` / `--cbx-muted` | `#E8EDF7` / `#7A899E` | texto principal e secundário |
| `--cbx-font-display` | Syne | títulos, rótulos em maiúsculas, botões |
| `--cbx-font-mono` | Space Mono | corpo de texto e números (como no sítio) |

## Camada semântica

O código usa só nomes semânticos (`--page`, `--surface-1..3`, `--text-primary`,
`--accent`, `--good`, `--warning`, `--critical`, `--series-1..5`, `--floor`, `--wall`...).
Há três variantes: escuro (omissão), claro (`data-theme="light"`) e alto contraste
(`data-contrast="high"`, em qualquer dos temas). As cores de estado são reservadas: nunca
servem de decoração.

Cores por departamento (camisas das personagens e marcas na lista da equipa):

| Departamento | Cor |
|---|---|
| Inteligência de Mercado | ciano |
| Mesa de Estratégia | verde-azulado (teal) |
| Risco e Controlo | vermelho |
| Execução | laranja |
| Investigação e Revisão | azul |
| Operações e Liderança | azul-marinho com acento amarelo |

## Tipos de letra

Syne e Space Mono estão em `panel/static/fonts/` (WOFF2, subconjuntos latin e latin-ext,
que cobrem o português). Licença SIL Open Font License 1.1: `fonts/OFL-Syne.txt` e
`fonts/OFL-SpaceMono.txt`. Nada é carregado de CDN.

## Logótipo

`panel/static/img/cyberbox-logo-256.png` e `-96.png` são o logótipo oficial recortado (sem a
margem transparente) e reduzido; `favicon-48.png` é o ícone do separador. Não alterar
cores, proporções nem o texto do logótipo; não o pôr sobre fundos que lhe tirem contraste
(no tema claro aparece sobre branco, no escuro sobre `--cbx-darker`).

## Regras

1. Nunca escrever um hexadecimal fora de `tokens.css` (excepção: o desenho das personagens e
   do mobiliário em `office/sprites.js` e `office/engine.js`, que usam a paleta da marca).
2. Texto sobre `--accent` usa `--text-on-accent`.
3. O selo REAL é sempre vermelho cheio; DEMO é contorno azul; SIMULAÇÃO é violeta.
4. Botões: Syne, maiúsculas, espaçamento de letras 0,06em, como no sítio.
5. Movimento: respeita `prefers-reduced-motion` e a preferência do utilizador.
