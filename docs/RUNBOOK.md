# Manual de operação

O que cada alerta significa e o que fazer. Os comandos são para `cmd` do Windows, a partir da pasta do projecto. Regra geral: as posições têm SL e TP no servidor do corretor, por isso quase nenhum alerta exige pressa. A excepção é `order_uncertain` quando há uma posição sem protecção.

## Comandos que vai usar

```cmd
.venv\Scripts\python mt5_bot.py --status
.venv\Scripts\python mt5_bot.py --diagnose --hours 24
.venv\Scripts\python mt5_bot.py --truth
.venv\Scripts\python mt5_bot.py --complexity-report
.venv\Scripts\python mt5_bot.py --board-report
.venv\Scripts\python mt5_bot.py --secret-scan
```

Para parar tudo com segurança, crie o ficheiro `KILL` na pasta do projecto:

```cmd
type nul > KILL
```

O bot fecha as suas posições e termina. Apague o ficheiro antes de voltar a arrancar. Para parar apenas novas entradas, use a pausa do painel: não fecha nada.

## Alertas

### `order_uncertain` (crítico)

**Significa:** uma ordem saiu e não há prova do que aconteceu (timeout, ligação, resposta vazia). O bot não reenvia e bloqueia novas entradas nesse símbolo.

**Faça:**
1. Normalmente nada: a reconciliação corre a cada ciclo e resolve-se sozinha em minutos.
2. Se durar mais de 5 minutos, abra o MT5 e veja se a posição existe e tem SL e TP.
3. Verifique a ligação no canto inferior direito do terminal.
4. No painel, abra "Máquina do tempo" e veja o estado da ordem.

Nunca abra manualmente uma posição para "compensar": a ordem pode ter sido executada.

### `broker_breaker_open` (crítico)

**Significa:** várias falhas de infraestrutura seguidas. Novas entradas param durante 5 minutos, e depois uma ordem de teste decide se volta ao normal. Rejeições normais do corretor não contam. Fechos e alterações de stop continuam a funcionar.

**Faça:** confirme que o terminal está ligado e que "Algo Trading" está activo. Se a ligação estiver boa e o alerta se repetir, veja `bot.log` e contacte o corretor. Sobrevive a reinícios de propósito.

### `research_only` (crítico)

**Significa:** a regra de paragem actuou. A expectativa líquida depois de custos tem o intervalo de confiança todo abaixo de zero. Não há novas ordens reais, as posições abertas seguem com os seus SL e TP, e a demo e a investigação continuam.

**Faça:**
1. Leia o relatório em `reports\<conta>\paragem_<data>.md`.
2. Veja o separador "Verdade": custo contra vantagem e distância à realidade.
3. Decida. Voltar a negociar em real é uma decisão sua, depois de ler o relatório:

```cmd
.venv\Scripts\python mt5_bot.py --reenable-real
```

Não reactive só para "recuperar". A regra existe para esse momento.

### `reality_gap` (aviso)

**Significa:** o real está a render menos do que a simulação dos mesmos sinais, ou a derrapagem ou a taxa de preenchimento saíram dos limites. As promoções de nível ficam congeladas e o modelo de custos é recalibrado.

**Faça:** veja "Verdade" e "Qualidade de execução". Spread por sessão e derrapagem por hora costumam mostrar a causa. Não precisa de agir: as promoções voltam quando a diferença regressa aos limites.

### `stress_breach` (aviso)

**Significa:** um cenário de stress com a carteira actual excederia o limite diário ou o de drawdown. O risco das novas entradas fica reduzido até ao teste seguinte. As posições abertas não são fechadas.

**Faça:** veja que cenário foi em "Risco de cauda". Se recorrer sempre com o mesmo símbolo, reduza a exposição a esse símbolo em `universe.candidates`.

### `derisk_ladder` (aviso)

**Significa:** o drawdown desceu um degrau da escada: x0,75 a 3%, x0,5 a 6%, x0,25 a 10%. Volta a subir um degrau de cada vez, com pelo menos 3 dias entre degraus.

**Faça:** nada. É o sistema a proteger-se. Não aumente limites para contrariar isto.

### `clock_drift` (aviso)

**Significa:** o relógio do PC afastou-se do relógio do corretor ou do servidor NTP. Regras de calendário usam o tempo do corretor, mas os registos e os relatórios usam o do PC.

**Faça:**

```cmd
w32tm /resync
```

### `backup_failed` (crítico)

**Significa:** a cópia diária falhou ou não passou a restauração de teste. Uma cópia marcada `.corrupt` não conta como cópia.

**Faça:** verifique espaço em disco e permissões em `state\<conta>\backups`. A negociação continua, mas sem rede de segurança: resolva no próprio dia.

## Situações frequentes

**Reiniciar depois de uma falha.** Arranque normalmente. O bot resolve ordens por acabar, regista posições desconhecidas e confirma SL e TP antes de negociar.

**Restaurar uma cópia.** Pare o bot, copie o ficheiro mais recente de `state\<conta>\backups` para `state\<conta>\mt5bot.sqlite` e apague `mt5bot.sqlite-wal` e `mt5bot.sqlite-shm` se existirem. Ao arrancar, a reconciliação alinha o diário com o corretor.

**Disco cheio.** O bot não envia ordens sem antes as gravar, por isso fica sem negociar em vez de negociar às cegas. Liberte espaço. Ticks antigos em `state\<conta>\data\ticks` podem ser apagados sem perda para a negociação.

**Métricas.** Com `"ops": {"metrics_enabled": true}`, o painel expõe `/metrics` no formato Prometheus.
