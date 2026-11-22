# Sistema de LOGS

Bot Discord em Python com discord.py e SQLite.

## Recursos
- Configuração de regras de views, curtidas e seguidores.
- Cadastro de números e comprovante, com cálculo automático.
- Carteira, histórico pessoal e ranking.
- Revisão por administradores e crédito automático após aprovação.
- Ajuste/desconto de saldo com justificativa e trilha de auditoria.
- Sinalização de comprovantes repetidos por hash/link.

## Instalação
1. Python 3.11+.
2. pip install -r requirements.txt
3. Configure DISCORD_TOKEN.
4. Configure LOGS_REVIEW_CHANNEL_ID e LOGS_AUDIT_CHANNEL_ID (recomendado).
5. Execute python main.py.

## Railway
Start command: python main.py
Anexe um Volume em /data e configure DB_PATH=/data/logs.db.
Configure DISCORD_TOKEN, LOGS_REVIEW_CHANNEL_ID e LOGS_AUDIT_CHANNEL_ID.

## Regras iniciais
20 views = 1 LOG; 100 curtidas = 20 LOGS; 100 seguidores = 1 LOG.
O cálculo considera unidades completas. A verificação automática só sinaliza duplicidades e alertas; a equipe deve conferir visualmente os comprovantes.