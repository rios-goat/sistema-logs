import os, json, hashlib, logging, sqlite3
from pathlib import Path
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands

TOKEN=os.getenv("DISCORD_TOKEN")
DB_PATH=os.getenv("DB_PATH","data/logs.db")
REVIEW_ID=int(os.getenv("LOGS_REVIEW_CHANNEL_ID","0") or 0)
AUDIT_ID=int(os.getenv("LOGS_AUDIT_CHANNEL_ID","0") or 0)
Path(DB_PATH).parent.mkdir(parents=True,exist_ok=True)
logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("SistemaLOGS")
def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def db():
    c=sqlite3.connect(DB_PATH,timeout=20); c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL"); return c
def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS wallets(user_id INTEGER PRIMARY KEY,balance INTEGER NOT NULL CHECK(balance>=0),updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS registrations(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,roblox TEXT NOT NULL,account TEXT NOT NULL,views INTEGER NOT NULL,likes INTEGER NOT NULL,followers INTEGER NOT NULL,logs INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'PENDENTE',proof_url TEXT,proof_hash TEXT,proof_type TEXT,flags TEXT NOT NULL DEFAULT '[]',reviewer INTEGER,reason TEXT,created TEXT NOT NULL,reviewed TEXT);
        CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,amount INTEGER NOT NULL,balance_after INTEGER NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,actor INTEGER NOT NULL,registration_id INTEGER,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proof_hashes(hash TEXT PRIMARY KEY,registration_id INTEGER NOT NULL,user_id INTEGER NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS member_responses(id INTEGER PRIMARY KEY AUTOINCREMENT,registration_id INTEGER NOT NULL,user_id INTEGER NOT NULL,response TEXT NOT NULL,created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS reg_user ON registrations(user_id,id);
        CREATE INDEX IF NOT EXISTS mov_user ON movements(user_id,id);
        """)
        cols={row[1] for row in c.execute("PRAGMA table_info(registrations)")}
        if "proof_type" not in cols: c.execute("ALTER TABLE registrations ADD COLUMN proof_type TEXT")
        defaults={"views_unit":20,"views_logs":1,"likes_unit":100,"likes_logs":20,"followers_unit":100,"followers_logs":1}
        for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",(k,v))
def rules():
    with db() as c: return {r["k"]:r["v"] for r in c.execute("SELECT * FROM settings")}
def calculate(v,l,f):
    r=rules(); return v//r["views_unit"]*r["views_logs"]+l//r["likes_unit"]*r["likes_logs"]+f//r["followers_unit"]*r["followers_logs"]
def balance(uid):
    with db() as c:
        r=c.execute("SELECT balance FROM wallets WHERE user_id=?",(uid,)).fetchone()
        return r["balance"] if r else 0
def movement(c,uid,amount,kind,reason,actor,rid=None):
    r=c.execute("SELECT balance FROM wallets WHERE user_id=?",(uid,)).fetchone()
    before=r["balance"] if r else 0; after=before+amount
    if after<0: raise ValueError("Saldo insuficiente.")
    c.execute("INSERT INTO wallets VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,updated=excluded.updated",(uid,after,now()))
    c.execute("INSERT INTO movements(user_id,amount,balance_after,kind,reason,actor,registration_id,created) VALUES(?,?,?,?,?,?,?,?)",(uid,amount,after,kind,reason,actor,rid,now()))
    return after
def admin():
    async def check(i):
        if not i.guild or not i.user.guild_permissions.administrator: raise app_commands.CheckFailure("Comando exclusivo para quem tem a permissão nativa Administrador.")
        return True
    return app_commands.check(check)
def registration(rid):
    with db() as c: return c.execute("SELECT * FROM registrations WHERE id=?",(rid,)).fetchone()
def approve(rid,actor,reason):
    with db() as c:
        c.execute("BEGIN IMMEDIATE"); r=c.execute("SELECT * FROM registrations WHERE id=?",(rid,)).fetchone()
        if not r: return False,"Cadastro não encontrado.",None,None
        if r["status"]!="PENDENTE": return False,"Esse cadastro já foi analisado.",None,None
        b=movement(c,r["user_id"],r["logs"],"CADASTRO_APROVADO",f"Cadastro #{rid} aprovado: {reason}",actor,rid)
        c.execute("UPDATE registrations SET status='APROVADO',reviewer=?,reason=?,reviewed=? WHERE id=?",(actor,reason,now(),rid))
    return True,f"Cadastro #{rid} aprovado: {r['logs']} LOGS creditados. Saldo: {b}.",r["user_id"],r["logs"]
def reject(rid,actor,reason):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM registrations WHERE id=?",(rid,)).fetchone()
        if not r: return False,"Cadastro não encontrado.",None,None
        if r["status"]!="PENDENTE": return False,"Esse cadastro já foi analisado.",None,None
        c.execute("UPDATE registrations SET status='REPROVADO',reviewer=?,reason=?,reviewed=? WHERE id=?",(actor,reason,now(),rid))
    return True,f"Cadastro #{rid} reprovado.",r["user_id"],0

async def audit(guild,message):
    if not AUDIT_ID: return
    try:
        channel=(guild.get_channel(AUDIT_ID) if guild else None) or await bot.fetch_channel(AUDIT_ID)
        await channel.send(message)
    except Exception:
        log.exception("Falha ao registrar auditoria.")

async def notify_member(bot,rid,approved,reason,logs):
    r=registration(rid)
    if not r: return
    try:
        user=bot.get_user(r["user_id"]) or await bot.fetch_user(r["user_id"])
        heading="## ✅ Seus LOGS foram aprovados!" if approved else "## ❌ Seu cadastro de LOGS foi reprovado"
        detail=f"**Cadastro:** #{rid}\n**Resultado:** {'Aprovado' if approved else 'Reprovado'}\n**LOGS creditados:** {logs if approved else 0}\n**Motivo/observação da equipe:** {reason}\n\nSe quiser contestar ou explicar algo, use o botão abaixo."
        await user.send(view=MemberDecisionView(rid,heading,detail))
    except discord.Forbidden:
        log.warning("Não foi possível enviar DM ao usuário %s (mensagens privadas fechadas).",r["user_id"])
    except Exception:
        log.exception("Falha ao enviar resultado por DM.")
async def post_review(bot,rid):
    r=registration(rid)
    if not r or not REVIEW_ID: return
    ch=bot.get_channel(REVIEW_ID)
    if not ch:
        try: ch=await bot.fetch_channel(REVIEW_ID)
        except Exception: log.exception("Canal de análise indisponível"); return
    flags=json.loads(r["flags"])
    content=f"## 📥 Cadastro de LOGS #{rid}\n**Membro:** <@{r['user_id']}>\n**Roblox:** {r['roblox']}\n**Conta social:** {r['account']}\n**Views:** {r['views']} • **Curtidas:** {r['likes']} • **Seguidores:** {r['followers']}\n**LOGS calculados:** {r['logs']}\n**Alertas:** {' | '.join(flags) if flags else 'Nenhuma duplicidade detectada; confira visualmente.'}"
    content+="\n\n*Alertas automáticos não comprovam fraude. A decisão exige conferência humana.*"
    await ch.send(view=ReviewView(rid,content,r["proof_url"],r["proof_type"]))
class ReviewReasonModal(discord.ui.Modal):
    def __init__(self,rid,approved):
        super().__init__(title="Motivo da aprovação" if approved else "Motivo da reprovação",timeout=300)
        self.rid=rid; self.approved=approved
        self.reason=discord.ui.TextInput(label="Motivo / observação",placeholder="Explique a decisão para o membro...",style=discord.TextStyle.paragraph,min_length=3,max_length=500,required=True)
        self.add_item(self.reason)
    async def on_submit(self,i):
        reason=str(self.reason.value).strip()
        if self.approved: ok,msg,uid,logs=approve(self.rid,i.user.id,reason)
        else: ok,msg,uid,logs=reject(self.rid,i.user.id,reason)
        await i.response.send_message(msg,ephemeral=True)
        if ok:
            await notify_member(i.client,self.rid,self.approved,reason,logs)
            await audit(i.guild,f"{'✅ Aprovado' if self.approved else '❌ Reprovado'}: cadastro #{self.rid}, membro <@{uid}>. Responsável: {i.user.mention}. Motivo: {reason}")
            if i.message:
                try: await i.message.edit(view=None)
                except Exception: log.exception("Não foi possível atualizar painel de análise.")
class ReviewView(discord.ui.LayoutView):
    def __init__(self,rid,content=None,proof_url=None,proof_type=None):
        super().__init__(timeout=None)
        yes=discord.ui.Button(label="Aprovar",style=discord.ButtonStyle.success,custom_id=f"logs:approve:{rid}")
        no=discord.ui.Button(label="Reprovar",style=discord.ButtonStyle.danger,custom_id=f"logs:reject:{rid}")
        async def yes_cb(i): await i.response.send_modal(ReviewReasonModal(rid,True))
        async def no_cb(i): await i.response.send_modal(ReviewReasonModal(rid,False))
        yes.callback=yes_cb; no.callback=no_cb
        items=[discord.ui.TextDisplay(content or f"## Revisão do cadastro #{rid}")]
        media_type=(proof_type or "").lower()
        image_ext=bool(proof_url and proof_url.split("?",1)[0].lower().endswith((".png",".jpg",".jpeg",".gif",".webp")))
        is_image=bool(proof_url and (media_type.startswith("image/") or image_ext))
        if is_image:
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(proof_url,description="Comprovante enviado")))
        items.append(discord.ui.Separator())
        buttons=[yes,no]
        if proof_url and not is_image:
            buttons.append(discord.ui.Button(label="Abrir comprovante",style=discord.ButtonStyle.link,url=proof_url))
        items.append(discord.ui.ActionRow(*buttons))
        self.add_item(discord.ui.Container(*items,accent_colour=0x9167FF))
    async def interaction_check(self,i):
        if not i.guild or not i.user.guild_permissions.administrator:
            await i.response.send_message("Somente administradores podem revisar.",ephemeral=True); return False
        return True
class MemberReplyModal(discord.ui.Modal):
    def __init__(self,rid):
        super().__init__(title=f"Resposta ao cadastro #{rid}",timeout=600); self.rid=rid
        self.reply=discord.ui.TextInput(label="Sua resposta",placeholder="Escreva sua explicação ou contestação...",style=discord.TextStyle.paragraph,min_length=3,max_length=1000,required=True)
        self.add_item(self.reply)
    async def on_submit(self,i):
        r=registration(self.rid)
        if not r or r["user_id"]!=i.user.id:
            return await i.response.send_message("Este cadastro não pertence a você.",ephemeral=True)
        answer=str(self.reply.value).strip()
        with db() as c: c.execute("INSERT INTO member_responses(registration_id,user_id,response,created) VALUES(?,?,?,?)",(self.rid,i.user.id,answer,now()))
        await i.response.send_message("Sua resposta foi enviada à equipe para análise.",ephemeral=True)
        notice=f"## 📨 Contestação do cadastro #{self.rid}\n**Membro:** <@{i.user.id}>\n**Resposta:** {answer}"
        for channel_id in (REVIEW_ID,AUDIT_ID):
            if channel_id:
                try:
                    ch=i.client.get_channel(channel_id) or await i.client.fetch_channel(channel_id)
                    await ch.send(view=SimpleNoticeView(notice))
                except Exception: log.exception("Falha ao encaminhar resposta do membro.")
class SimpleNoticeView(discord.ui.LayoutView):
    def __init__(self,message):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(message),accent_colour=0x3498DB))
class MemberDecisionView(discord.ui.LayoutView):
    def __init__(self,rid,heading=None,detail=None):
        super().__init__(timeout=None)
        button=discord.ui.Button(label="Responder à decisão",style=discord.ButtonStyle.primary,custom_id=f"logs:reply:{rid}")
        async def callback(i): await i.response.send_modal(MemberReplyModal(rid))
        button.callback=callback
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(heading or f"## Resultado do cadastro #{rid}"),discord.ui.Separator(),discord.ui.TextDisplay(detail or "Use o botão abaixo para responder."),discord.ui.ActionRow(button),accent_colour=0x9167FF))
class Bot(commands.Bot):
    async def setup_hook(self):
        init_db()
        with db() as c:
            pending=[x["id"] for x in c.execute("SELECT id FROM registrations WHERE status='PENDENTE'")]
            reviewed=[x["id"] for x in c.execute("SELECT id FROM registrations WHERE status IN ('APROVADO','REPROVADO')")]
        for rid in pending: self.add_view(ReviewView(rid))
        for rid in reviewed: self.add_view(MemberDecisionView(rid))
        await self.tree.sync()
intents=discord.Intents.default()
bot=Bot(command_prefix="!",intents=intents)
@bot.event
async def on_ready(): log.info("Conectado como %s",bot.user)

@bot.tree.command(name="atualizar_conta",description="Configura as regras de conversão de LOGS.")
@admin()
async def atualizar_conta(i:discord.Interaction,views_unidade:app_commands.Range[int,1,100000000],views_logs:app_commands.Range[int,0,1000000],curtidas_unidade:app_commands.Range[int,1,100000000],curtidas_logs:app_commands.Range[int,0,1000000],seguidores_unidade:app_commands.Range[int,1,100000000],seguidores_logs:app_commands.Range[int,0,1000000]):
    vals={"views_unit":views_unidade,"views_logs":views_logs,"likes_unit":curtidas_unidade,"likes_logs":curtidas_logs,"followers_unit":seguidores_unidade,"followers_logs":seguidores_logs}
    with db() as c:
        for k,v in vals.items(): c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(k,v))
    await i.response.send_message(f"Regras salvas: {views_unidade} views = {views_logs} LOGS; {curtidas_unidade} curtidas = {curtidas_logs} LOGS; {seguidores_unidade} seguidores = {seguidores_logs} LOGS.",ephemeral=True)
    await audit(i.guild,f"⚙️ {i.user.mention} atualizou as regras.")
@bot.tree.command(name="registrar_logs",description="Envia números e comprovante para análise.")
async def registrar_logs(i:discord.Interaction,roblox_nick:app_commands.Range[str,1,80],conta_social:app_commands.Range[str,1,150],curtidas:app_commands.Range[int,0,100000000],seguidores:app_commands.Range[int,0,100000000],views:app_commands.Range[int,0,100000000]=0,comprovante:discord.Attachment=None,link_comprovante:str=None):
    if not comprovante and not link_comprovante: return await i.response.send_message("Envie um arquivo ou link de comprovante.",ephemeral=True)
    proof_hash=None; url=link_comprovante.strip() if link_comprovante else None
    if comprovante:
        if comprovante.size>8*1024*1024: return await i.response.send_message("Arquivo maior que 8 MB.",ephemeral=True)
        try: proof_hash=hashlib.sha256(await comprovante.read()).hexdigest(); url=comprovante.url; proof_type=comprovante.content_type or ""
        except Exception: return await i.response.send_message("Não consegui ler o arquivo.",ephemeral=True)
    total=calculate(views,curtidas,seguidores); flags=[]
    with db() as c:
        if proof_hash:
            old=c.execute("SELECT registration_id,user_id FROM proof_hashes WHERE hash=?",(proof_hash,)).fetchone()
            if old: flags.append(f"Arquivo idêntico já usado no cadastro #{old['registration_id']} (usuário {old['user_id']}).")
        if url:
            old=c.execute("SELECT id FROM registrations WHERE proof_url=? LIMIT 1",(url,)).fetchone()
            if old: flags.append(f"Link já usado no cadastro #{old['id']}.")
        if not any((views,curtidas,seguidores)): flags.append("Todos os números estão zerados.")
        if max(views,curtidas,seguidores)>100000000: flags.append("Valor muito alto.")
        cur=c.execute("INSERT INTO registrations(user_id,roblox,account,views,likes,followers,logs,proof_url,proof_hash,proof_type,flags,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(i.user.id,roblox_nick,conta_social,views,curtidas,seguidores,total,url,proof_hash,proof_type,json.dumps(flags,ensure_ascii=False),now()))
        rid=cur.lastrowid
        if proof_hash: c.execute("INSERT OR IGNORE INTO proof_hashes VALUES(?,?,?,?)",(proof_hash,rid,i.user.id,now()))
    await i.response.send_message(f"Cadastro #{rid} enviado! Cálculo: {total} LOGS. Status: PENDENTE; o saldo só entra após aprovação.",ephemeral=True)
    await post_review(bot,rid); await audit(i.guild,f"📥 Novo cadastro #{rid} enviado por {i.user.mention}.")
@bot.tree.command(name="carteira",description="Consulta seu saldo de LOGS.")
async def carteira(i): await i.response.send_message(f"Sua carteira: **{balance(i.user.id)} LOGS**.",ephemeral=True)
@bot.tree.command(name="meus_logs",description="Mostra seus cadastros e movimentações recentes.")
async def meus_logs(i):
    with db() as c:
        regs=c.execute("SELECT id,logs,status FROM registrations WHERE user_id=? ORDER BY id DESC LIMIT 8",(i.user.id,)).fetchall()
        movs=c.execute("SELECT amount,kind,reason FROM movements WHERE user_id=? ORDER BY id DESC LIMIT 8",(i.user.id,)).fetchall()
    lines=[f"Cadastro #{r['id']}: {r['logs']} LOGS — {r['status']}" for r in regs] or ["Nenhum cadastro."]
    lines+=["","Movimentações:"]+[f"{m['amount']:+} — {m['kind']} ({m['reason']})" for m in movs]
    await i.response.send_message("\\n".join(lines),ephemeral=True)
@bot.tree.command(name="ranking_logs",description="Exibe os 10 maiores saldos.")
async def ranking_logs(i):
    with db() as c: rows=c.execute("SELECT user_id,balance FROM wallets WHERE balance>0 ORDER BY balance DESC LIMIT 10").fetchall()
    await i.response.send_message("🏆 Ranking de LOGS\\n"+("\\n".join(f"{n}. <@{r['user_id']}> — {r['balance']} LOGS" for n,r in enumerate(rows,1)) if rows else "Ainda vazio."))
@bot.tree.command(name="editar_saldo",description="Adiciona ou define o saldo de um membro.")
@admin()
@app_commands.choices(modo=[app_commands.Choice(name="Adicionar",value="adicionar"),app_commands.Choice(name="Definir",value="definir")])
async def editar_saldo(i,membro:discord.Member,quantidade:app_commands.Range[int,0,1000000000],modo:app_commands.Choice[str],motivo:app_commands.Range[str,1,250]):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        old=c.execute("SELECT balance FROM wallets WHERE user_id=?",(membro.id,)).fetchone(); before=old["balance"] if old else 0
        delta=quantidade-before if modo.value=="definir" else quantidade
        b=movement(c,membro.id,delta,"SALDO_DEFINIDO" if modo.value=="definir" else "SALDO_ADICIONADO",motivo,i.user.id)
    await i.response.send_message(f"Saldo de {membro.mention}: **{b} LOGS**.",ephemeral=True); await audit(i.guild,f"🛠️ {i.user.mention} ajustou saldo de {membro.mention}. Motivo: {motivo}")
@bot.tree.command(name="descontar_saldo",description="Desconta LOGS da carteira.")
@admin()
async def descontar_saldo(i,membro:discord.Member,quantidade:app_commands.Range[int,1,1000000000],motivo:app_commands.Range[str,1,250]):
    try:
        with db() as c: c.execute("BEGIN IMMEDIATE"); b=movement(c,membro.id,-quantidade,"DESCONTO",motivo,i.user.id)
    except ValueError as e: return await i.response.send_message(str(e),ephemeral=True)
    await i.response.send_message(f"Descontados {quantidade} LOGS. Saldo: {b}.",ephemeral=True); await audit(i.guild,f"➖ {i.user.mention} descontou {quantidade} de {membro.mention}. Motivo: {motivo}")
@bot.tree.command(name="consultar_logs",description="Consulta saldo de qualquer membro.")
@admin()
async def consultar_logs(i,membro:discord.Member): await i.response.send_message(f"{membro.mention}: **{balance(membro.id)} LOGS**.",ephemeral=True)
@bot.tree.command(name="historico_logs",description="Consulta as últimas movimentações.")
@admin()
async def historico_logs(i):
    with db() as c: rows=c.execute("SELECT user_id,amount,balance_after,kind,actor FROM movements ORDER BY id DESC LIMIT 20").fetchall()
    await i.response.send_message("\\n".join(f"<@{r['user_id']}> {r['amount']:+} → {r['balance_after']} ({r['kind']}) por <@{r['actor']}>" for r in rows) or "Sem movimentações.",ephemeral=True)
@bot.tree.command(name="analisar_logs",description="Analisa dados e alertas de um cadastro.")
@admin()
async def analisar_logs(i,cadastro_id:app_commands.Range[int,1,2147483647]):
    with db() as c: r=c.execute("SELECT * FROM registrations WHERE id=?",(cadastro_id,)).fetchone()
    if not r: return await i.response.send_message("Cadastro não encontrado.",ephemeral=True)
    e=discord.Embed(title=f"Análise #{cadastro_id}",description=f"Membro: <@{r['user_id']}>\\nRoblox: {r['roblox']}\\nConta: {r['account']}\\nViews/curtidas/seguidores: {r['views']}/{r['likes']}/{r['followers']}\\nCálculo: {r['logs']} LOGS\\nStatus: {r['status']}\\nAlertas: "+(" | ".join(json.loads(r["flags"])) or "Nenhum alerta automático; confira visualmente."))
    if r["proof_url"]: e.add_field(name="Comprovante",value=f"[Abrir]({r['proof_url']})")
    await i.response.send_message(embed=e,ephemeral=True)
@bot.tree.command(name="aprovar_logs",description="Aprova cadastro e credita a carteira.")
@admin()
async def aprovar_logs(i,cadastro_id:app_commands.Range[int,1,2147483647],motivo:app_commands.Range[str,3,500]):
    ok,msg,uid,logs=approve(cadastro_id,i.user.id,motivo); await i.response.send_message(msg,ephemeral=True)
    if ok:
        await notify_member(bot,cadastro_id,True,motivo,logs)
        await audit(i.guild,f"✅ {i.user.mention} aprovou cadastro #{cadastro_id}. Motivo: {motivo}")
@bot.tree.command(name="reprovar_logs",description="Reprova cadastro com motivo.")
@admin()
async def reprovar_logs(i,cadastro_id:app_commands.Range[int,1,2147483647],motivo:app_commands.Range[str,3,500]):
    ok,msg,uid,logs=reject(cadastro_id,i.user.id,motivo); await i.response.send_message(msg,ephemeral=True)
    if ok:
        await notify_member(bot,cadastro_id,False,motivo,logs)
        await audit(i.guild,f"❌ {i.user.mention} reprovou cadastro #{cadastro_id}: {motivo}")
if not TOKEN: raise RuntimeError("Configure DISCORD_TOKEN nas variáveis de ambiente.")
bot.run(TOKEN)
