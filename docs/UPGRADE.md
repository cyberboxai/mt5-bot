# Actualizar para a versão 3.1 (e voltar atrás)

A versão 3.1 é **um único sistema**: um ponto de entrada (`mt5_bot.py`), um motor, uma base
de dados por conta (`mt5bot.sqlite`), um esquema de configuração (`config_version` "3.1") e
um painel. Não existe "modo v2" nem "modo v3.0" para escolher. A actualização guarda uma
cópia de segurança antes de mexer em qualquer coisa e mantém todo o histórico (trades,
dossiers, auditoria).

Todos os comandos são para a janela `cmd` do Windows, na pasta do projecto
(`C:\mt5-bot` neste guia).

## O que muda

| Antes (3.0) | Depois (3.1) |
|---|---|
| `registry.sqlite` + `office.sqlite` por conta | `mt5bot.sqlite` por conta (as duas antigas ficam com o sufixo `.v30`, não são apagadas) |
| Campeão por símbolo, desafiantes, `NoTrade` por omissão | Braços (estratégia + parâmetros + símbolo + período) com níveis de evidência T0 a T3 |
| `risk_per_trade_pct_*`, `max_daily_loss_pct_*`... como valores principais | `risk_profile` (conservative, balanced, aggressive); os valores antigos passam a ser **tectos** que só apertam |
| Portões walk-forward decidem promoções | Portões são alertas; a evidência (sombra + real + investigação descontada) decide |
| `--optimize-once` | `--research-once` (o nome antigo continua a funcionar, escondido) |

As chaves antigas do `config.json` são aceites. As que deixaram de ter efeito são movidas
para um bloco `_legacy_v30` com um aviso no registo; nenhuma é descartada em silêncio. O
bot escreve ao lado do seu ficheiro um `config.migrated.json` com o resultado, para rever.

## Passo a passo

1. **Pare o bot e o supervisor.** Na janela do supervisor (ou do bot): **Ctrl+C**. Confirme
   que já não corre:
   ```
   tasklist /FI "IMAGENAME eq python.exe"
   ```
   **Sucesso:** nenhuma linha de `python.exe` do bot (outras podem existir, de outros programas).

2. **Copie a instalação inteira** (código, ambiente virtual, estado e configuração). Esta cópia
   é o que permite voltar atrás no código:
   ```
   robocopy C:\mt5-bot C:\mt5-bot-v30 /E
   ```
   **Sucesso:** o resumo final mostra `Failed: 0` (o código de saída 1 do `robocopy` significa
   "ficheiros copiados", é normal).

3. **Copie os ficheiros novos** do projecto para `C:\mt5-bot`, por cima dos antigos,
   **excepto** o seu `config.json` e as pastas `state`, `reports` e `.venv`.
   Apague estes dois ficheiros, que já não existem na 3.1 (se não os apagar, nada acontece:
   já não são importados):
   ```
   del C:\mt5-bot\mt5bot\engine.py
   del C:\mt5-bot\mt5bot\cycle.py
   ```

4. **Actualize as dependências** (a 3.1 não acrescenta nenhuma obrigatória):
   ```
   cd /d C:\mt5-bot
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

5. **Corra os testes:**
   ```
   pip install -r requirements-dev.txt
   pytest -q
   ```
   **Sucesso:** a última linha diz `... passed, ... skipped` sem `failed`.

6. **Migre o estado** (faz cópia de segurança e junta as bases de dados de cada conta):
   ```
   python mt5_bot.py --migrate
   ```
   **Sucesso:** aparece `Backup: C:\mt5-bot\backups\upgrade_AAAAMMDDTHHMMSSZ` e, para cada
   conta, a acção feita, os braços criados a partir do histórico e a versão do esquema da base
   de dados. Numa conta já migrada a acção é `none` (a cópia de segurança é feita na mesma).
   Se saltar este passo, o bot faz o mesmo sozinho no primeiro arranque.

7. **Reveja a configuração.** Se o bot escreveu `config.migrated.json`, abra-o ao lado do
   `config.json`. Os avisos explicam cada chave movida. Para adoptar o ficheiro migrado:
   ```
   copy /Y config.migrated.json config.json
   ```
   Para escolher o perfil de risco, edite `"risk_profile"` (ver
   [EVIDENCE_AND_RISK.md](EVIDENCE_AND_RISK.md)). Em conta real comece por `"conservative"`.

8. **Primeiro arranque em seco, numa conta demo:**
   ```
   python mt5_bot.py --dry-run
   ```
   Espere alguns minutos e, noutra janela:
   ```
   python mt5_bot.py --status
   python mt5_bot.py --diagnose --hours 1
   ```
   **Sucesso:** o estado mostra braços por nível e a linha de estado em português; o
   diagnóstico mostra barras avaliadas e sinais. Pare com Ctrl+C.

9. **Arranque normal:**
   ```
   start_supervisor.bat
   ```
   O arranque automático do Windows (`install_autostart.bat`) não precisa de ser refeito.

## Voltar atrás (rollback)

Use se a 3.1 não se comportar como espera. Nada é apagado: o estado da 3.1 é posto de lado
numa pasta própria.

1. Pare o bot e o supervisor (Ctrl+C) e confirme com `tasklist` como no passo 1.
2. Reponha o estado e a configuração de antes da migração:
   ```
   cd /d C:\mt5-bot
   .venv\Scripts\activate
   python mt5_bot.py --rollback-upgrade
   ```
   **Sucesso:** mostra de que cópia restaurou e para onde moveu o estado da 3.1
   (`backups\rolled_back_...`). Se o bot parecer ainda a correr (`health.json` actualizado há
   menos de 2 minutos), o comando recusa; pare-o ou, se tem a certeza, junte `--force`.
   Para escolher outra cópia: `python mt5_bot.py --rollback-upgrade --backup upgrade_AAAAMMDDTHHMMSSZ`.
3. Reponha o código 3.0 trocando as pastas:
   ```
   cd /d C:\
   ren C:\mt5-bot mt5-bot-v31
   ren C:\mt5-bot-v30 mt5-bot
   ```
   A pasta `C:\mt5-bot` volta a ser exactamente a instalação 3.0 copiada no passo 2 (com o
   seu próprio ambiente virtual, estado e configuração). A pasta `C:\mt5-bot-v31` fica intacta,
   com o estado da 3.1 em `backups\rolled_back_...`.
4. Arranque: `cd /d C:\mt5-bot` e `start_supervisor.bat`.

**Nota:** os trades abertos durante o período na 3.1 continuam protegidos pelo SL/TP no
servidor. Ao arrancar, a 3.0 reconcilia as posições abertas com o `magic` do bot.

Para voltar a tentar a 3.1 mais tarde, faça de novo o guia a partir do passo 1.

## Perguntas frequentes

**Perco o histórico?** Não. Trades, dossiers, pesos da equipa e auditoria são copiados para
`mt5bot.sqlite`; os trades da 3.0 ganham um braço (`legacy`) para alimentarem a evidência.
As bases antigas ficam como `registry.sqlite.v30` e `office.sqlite.v30`.

**E se tiver várias contas?** A migração trata todas as pastas em `state\`.

**O painel antigo continua a funcionar?** O painel faz parte da mesma versão; depois da
actualização é o CYBERBOX Trading Office novo, com o separador
**Porque não estou a negociar?**.
