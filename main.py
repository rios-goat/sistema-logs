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
REGISTRATION_CHANNEL_ID=1566053094029135923
GUILD_ID=int(os.getenv("GUILD_ID","1554264275684565103") or 0)
LOGS_STAFF_ROLE_ID=1569043605228748940
LOGS_STAFF_USER_ID=1340377944215785502
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
        CREATE TABLE IF NOT EXISTS live_registrations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,roblox TEXT NOT NULL,discord_nick TEXT NOT NULL,comex_role TEXT NOT NULL,
            views INTEGER NOT NULL,likes INTEGER NOT NULL,logs INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'PENDENTE',
            proof_url TEXT NOT NULL,proof_hash TEXT,proof_type TEXT,flags TEXT NOT NULL DEFAULT '[]',
            reviewer INTEGER,reason TEXT,created TEXT NOT NULL,reviewed TEXT
        );
        CREATE TABLE IF NOT EXISTS live_proof_hashes(hash TEXT PRIMARY KEY,live_id INTEGER NOT NULL,user_id INTEGER NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS registration_announcements(kind TEXT NOT NULL,registration_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,message_id INTEGER NOT NULL,PRIMARY KEY(kind,registration_id));
        CREATE TABLE IF NOT EXISTS log_transfers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, reason TEXT NOT NULL,
            sender_accepted INTEGER NOT NULL DEFAULT 0,
            receiver_accepted INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'PENDENTE',
            reviewer_id INTEGER, created TEXT NOT NULL, reviewed TEXT
        );
        CREATE INDEX IF NOT EXISTS live_created ON live_registrations(created,status);

        CREATE INDEX IF NOT EXISTS reg_user ON registrations(user_id,id);
        CREATE INDEX IF NOT EXISTS mov_user ON movements(user_id,id);
        """)
        cols={row[1] for row in c.execute("PRAGMA table_info(registrations)")}
        if "proof_type" not in cols: c.execute("ALTER TABLE registrations ADD COLUMN proof_type TEXT")
        defaults={"views_unit":20,"views_logs":1,"likes_unit":5,"likes_logs":1,"followers_unit":100,"followers_logs":1,"live_views_unit":20,"live_views_logs":1,"live_likes_unit":100,"live_likes_logs":20}
        for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",(k,v))
        # Migração única: atualiza instalações existentes para 5 curtidas = 1 LOG.
        if not c.execute("SELECT 1 FROM settings WHERE k='likes_rule_v2'").fetchone():
            c.execute("INSERT INTO settings(k,v) VALUES('likes_unit',5) ON CONFLICT(k) DO UPDATE SET v=5")
            c.execute("INSERT INTO settings(k,v) VALUES('likes_logs',1) ON CONFLICT(k) DO UPDATE SET v=1")
            c.execute("INSERT INTO settings(k,v) VALUES('likes_rule_v2',1)")
def rules():
    with db() as c: return {r["k"]:r["v"] for r in c.execute("SELECT * FROM settings")}
def calculate(v,l,f):
    r=rules(); return l*r["likes_logs"]//r["likes_unit"]
def calculate_live(views):
    r=rules()
    return views//r["live_views_unit"]*r["live_views_logs"]
def live_registration(rid):
    with db() as c: return c.execute("SELECT * FROM live_registrations WHERE id=?",(rid,)).fetchone()
def approve_live(rid,actor,reason):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM live_registrations WHERE id=?",(rid,)).fetchone()
        if not r: return False,"Cadastro LIVE não encontrado.",None,None
        if r["status"]!="PENDENTE": return False,"Esse cadastro LIVE já foi analisado.",None,None
        b=movement(c,r["user_id"],r["logs"],"LIVE_APROVADA",f"Cadastro LIVE #{rid} aprovado: {reason}",actor,rid)
        c.execute("UPDATE live_registrations SET status='APROVADO',reviewer=?,reason=?,reviewed=? WHERE id=?",(actor,reason,now(),rid))
    return True,f"Cadastro LIVE #{rid} aprovado: {r['logs']} LOGS creditados. Saldo: {b}.",r["user_id"],r["logs"]
def reject_live(rid,actor,reason):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM live_registrations WHERE id=?",(rid,)).fetchone()
        if not r: return False,"Cadastro LIVE não encontrado.",None,None
        if r["status"]!="PENDENTE": return False,"Esse cadastro LIVE já foi analisado.",None,None
        c.execute("UPDATE live_registrations SET status='REPROVADO',reviewer=?,reason=?,reviewed=? WHERE id=?",(actor,reason,now(),rid))
    return True,f"Cadastro LIVE #{rid} reprovado.",r["user_id"],0
async def post_live_review(rid):
    r=live_registration(rid)
    if not r or not REVIEW_ID: return
    ch=bot.get_channel(REVIEW_ID)
    if not ch:
        try: ch=await bot.fetch_channel(REVIEW_ID)
        except Exception: log.exception("Canal de análise LIVE indisponível"); return
    flags=json.loads(r["flags"])
    media=(r["proof_type"] or "").lower()
    image_ext=r["proof_url"].split("?",1)[0].lower().endswith((".png",".jpg",".jpeg",".gif",".webp"))
    is_image=media.startswith("image/") or image_ext
    bot_view=LiveReviewView(rid,r["user_id"],r["roblox"],r["discord_nick"],r["comex_role"],r["views"],r["logs"],flags,r["proof_url"],is_image)
    await ch.send(view=bot_view)
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
def logs_staff_member(member):
    if not member: return False
    if member.id==LOGS_STAFF_USER_ID: return True
    if getattr(member,"guild_permissions",None) and member.guild_permissions.administrator: return True
    return any(role.id==LOGS_STAFF_ROLE_ID for role in getattr(member,"roles",[]))
def logs_staff():
    async def check(i):
        if not i.guild or not logs_staff_member(i.user):
            raise app_commands.CheckFailure("Apenas administradores, o cargo autorizado ou o usuário autorizado podem usar esta função.")
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
        await channel.send(view=SimpleNoticeView(f"## 📋 Sistema de LOGS • Auditoria\n{message}\n\n-# Registro automático • {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}"))
    except Exception:
        log.exception("Falha ao registrar auditoria.")

def registration_embed(kind,rid,status="PENDENTE",reason=None,reviewer=None):
    with db() as c:
        if kind=="live":
            r=c.execute("SELECT * FROM live_registrations WHERE id=?",(rid,)).fetchone()
            if not r: return None
            e=discord.Embed(title=f"🔴 Registro de LIVE • #{rid}",color=0xF1C40F if status=="PENDENTE" else (0x2ECC71 if status=="APROVADO" else 0xE74C3C))
            e.add_field(name="Membro",value=f"<@{r['user_id']}>",inline=False)
            e.add_field(name="Nick Roblox",value=r["roblox"],inline=True)
            e.add_field(name="Nick Discord",value=r["discord_nick"],inline=True)
            e.add_field(name="Cargo COMEX",value=r["comex_role"],inline=True)
            e.add_field(name="Visualizações",value=f"{r['views']:,}",inline=True)
            e.add_field(name="LOGS preliminares",value=f"{r['logs']:,}",inline=True)
        else:
            r=c.execute("SELECT * FROM registrations WHERE id=?",(rid,)).fetchone()
            if not r: return None
            e=discord.Embed(title=f"📥 Registro de vídeo • #{rid}",color=0xF1C40F if status=="PENDENTE" else (0x2ECC71 if status=="APROVADO" else 0xE74C3C))
            e.add_field(name="Membro",value=f"<@{r['user_id']}>",inline=False)
            e.add_field(name="Nick Roblox",value=r["roblox"],inline=True)
            e.add_field(name="Conta social",value=r["account"],inline=True)
            e.add_field(name="Visualizações",value=f"{r['views']:,}",inline=True)
            e.add_field(name="Curtidas",value=f"{r['likes']:,}",inline=True)
            e.add_field(name="Seguidores",value=f"{r['followers']:,}",inline=True)
            e.add_field(name="LOGS preliminares",value=f"{r['logs']:,}",inline=True)
    label={"PENDENTE":"🟡 Pendente de análise","APROVADO":"✅ ACEITO","REPROVADO":"❌ NÃO ACEITO"}.get(status,status)
    e.add_field(name="Status",value=label,inline=False)
    if reason: e.add_field(name="Motivo / observação",value=reason[:1024],inline=False)
    if reviewer: e.add_field(name="Analisado por",value=f"<@{reviewer}>",inline=False)
    e.set_footer(text="Sistema de LOGS • Atualizado automaticamente")
    return e

async def post_registration_notice(guild,kind,rid):
    try:
        channel=(guild.get_channel(REGISTRATION_CHANNEL_ID) if guild else None) or await bot.fetch_channel(REGISTRATION_CHANNEL_ID)
        message=await channel.send(embed=registration_embed(kind,rid))
        with db() as c:
            c.execute("INSERT OR REPLACE INTO registration_announcements(kind,registration_id,channel_id,message_id) VALUES(?,?,?,?)",(kind,rid,channel.id,message.id))
    except Exception:
        log.exception("Falha ao publicar aviso de novo cadastro no canal de registros.")

async def update_registration_notice(kind,rid,status,reason,reviewer):
    try:
        with db() as c:
            saved=c.execute("SELECT channel_id,message_id FROM registration_announcements WHERE kind=? AND registration_id=?",(kind,rid)).fetchone()
        if not saved: return
        channel=bot.get_channel(saved["channel_id"]) or await bot.fetch_channel(saved["channel_id"])
        message=await channel.fetch_message(saved["message_id"])
        await message.edit(embed=registration_embed(kind,rid,status,reason,reviewer),view=None)
    except discord.NotFound:
        log.warning("Aviso público do cadastro %s #%s não foi encontrado.",kind,rid)
    except Exception:
        log.exception("Falha ao atualizar status público do cadastro %s #%s.",kind,rid)

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
    stats=f"👁️ **Views:** {r['views']:,}\n❤️ **Curtidas:** {r['likes']:,}\n👥 **Seguidores:** {r['followers']:,}"
    verification="\n".join(f"• {flag}" for flag in flags) if flags else "✅ Nenhuma duplicidade detectada.\nA conferência visual continua necessária."
    media_type=(r["proof_type"] or "").lower()
    image_ext=bool(r["proof_url"] and r["proof_url"].split("?",1)[0].lower().endswith((".png",".jpg",".jpeg",".gif",".webp")))
    is_image=bool(r["proof_url"] and (media_type.startswith("image/") or image_ext))
    await ch.send(view=ReviewView(rid,r["user_id"],r["roblox"],r["account"],stats,r["logs"],verification,r["proof_url"],is_image))
class LiveReviewModal(discord.ui.Modal):
    def __init__(self,rid,approved):
        super().__init__(title="Aprovar LIVE" if approved else "Reprovar LIVE",timeout=300)
        self.rid=rid; self.approved=approved
        self.reason=discord.ui.TextInput(label="Motivo / observação",placeholder="Explique a decisão...",style=discord.TextStyle.paragraph,min_length=3,max_length=500,required=True)
        self.add_item(self.reason)
    async def on_submit(self,i):
        reason=str(self.reason.value).strip()
        ok,msg,uid,logs=approve_live(self.rid,i.user.id,reason) if self.approved else reject_live(self.rid,i.user.id,reason)
        await i.response.send_message(msg,ephemeral=True)
        if ok:
            await update_registration_notice("live",self.rid,"APROVADO" if self.approved else "REPROVADO",reason,i.user.id)
            heading="## ✅ Sua LIVE foi aprovada!" if self.approved else "## ❌ Seu cadastro de LIVE foi reprovado"
            detail=f"**Cadastro LIVE:** #{self.rid}\n**LOGS creditados:** {logs if self.approved else 0}\n**Motivo:** {reason}"
            try:
                user=i.client.get_user(uid) or await i.client.fetch_user(uid)
                await user.send(view=SimpleNoticeView(heading+"\n"+detail))
            except Exception: log.exception("Falha ao notificar membro do cadastro LIVE.")
            await audit(i.guild,f"{'✅ LIVE aprovada' if self.approved else '❌ LIVE reprovada'}: cadastro #{self.rid}, membro <@{uid}>. Responsável: {i.user.mention}. Motivo: {reason}")
            if i.message:
                try: await i.message.edit(view=None)
                except Exception: log.exception("Falha ao atualizar painel LIVE.")
class LiveReviewView(discord.ui.LayoutView):
    def __init__(self,rid,user_id,roblox,discord_nick,role,views,logs,flags,proof_url,is_image):
        super().__init__(timeout=None)
        yes=discord.ui.Button(label="Aprovar",style=discord.ButtonStyle.success,custom_id=f"logs:live:approve:{rid}",emoji="✅")
        no=discord.ui.Button(label="Reprovar",style=discord.ButtonStyle.danger,custom_id=f"logs:live:reject:{rid}",emoji="❌")
        async def yes_cb(i): await i.response.send_modal(LiveReviewModal(rid,True))
        async def no_cb(i): await i.response.send_modal(LiveReviewModal(rid,False))
        yes.callback=yes_cb; no.callback=no_cb
        items=[discord.ui.TextDisplay(f"# 🔴 Cadastro de LIVE #{rid}\n**Responsável:** <@{user_id}>\n**Nick Roblox:** {roblox}\n**Nick Discord:** {discord_nick}\n**Cargo COMEX:** {role}"),
            discord.ui.Separator(),discord.ui.TextDisplay(f"### 📊 Resultado da LIVE\n👁️ **Visualizações:** {views:,}\n💰 **LOGS calculados:** {logs}"),
            discord.ui.TextDisplay("### 🛡️ Verificação\n"+("\n".join("• "+x for x in flags) if flags else "✅ Nenhuma duplicidade detectada; confira a comprovação."))]
        if is_image: items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(proof_url,description="Comprovação da LIVE")))
        items.append(discord.ui.Separator())
        buttons=[yes,no]
        if not is_image: buttons.append(discord.ui.Button(label="Abrir comprovação",style=discord.ButtonStyle.link,url=proof_url,emoji="📎"))
        items.extend([discord.ui.ActionRow(*buttons),discord.ui.TextDisplay("-# Sistema de LOGS • Revisão manual obrigatória")])
        self.add_item(discord.ui.Container(*items,accent_colour=0xE74C3C))
    async def interaction_check(self,i):
        if not i.guild or not logs_staff_member(i.user):
            await i.response.send_message("Somente administradores, o cargo autorizado ou o usuário autorizado podem revisar.",ephemeral=True); return False
        return True
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
            await update_registration_notice("video",self.rid,"APROVADO" if self.approved else "REPROVADO",reason,i.user.id)
            await notify_member(i.client,self.rid,self.approved,reason,logs)
            await audit(i.guild,f"{'✅ Aprovado' if self.approved else '❌ Reprovado'}: cadastro #{self.rid}, membro <@{uid}>. Responsável: {i.user.mention}. Motivo: {reason}")
            if i.message:
                try: await i.message.edit(view=None)
                except Exception: log.exception("Não foi possível atualizar painel de análise.")
class ReviewView(discord.ui.LayoutView):
    def __init__(self,rid,user_id,roblox,account,stats,logs,verification,proof_url=None,is_image=False):
        super().__init__(timeout=None)
        yes=discord.ui.Button(label="Aprovar",style=discord.ButtonStyle.success,custom_id=f"logs:approve:{rid}",emoji="✅")
        no=discord.ui.Button(label="Reprovar",style=discord.ButtonStyle.danger,custom_id=f"logs:reject:{rid}",emoji="❌")
        async def yes_cb(i): await i.response.send_modal(ReviewReasonModal(rid,True))
        async def no_cb(i): await i.response.send_modal(ReviewReasonModal(rid,False))
        yes.callback=yes_cb; no.callback=no_cb
        items=[
            discord.ui.TextDisplay(f"# 📥 Cadastro de LOGS #{rid}\n**Membro:** <@{user_id}>\n**Roblox:** {roblox}\n**Conta social:** {account}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"### 📊 Estatísticas\n{stats}"),
            discord.ui.TextDisplay(f"### 💰 Resultado\n**{logs} LOGS**"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"### 🛡️ Verificação\n{verification}")
        ]
        if is_image and proof_url:
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(proof_url,description="Comprovante enviado")))
        items.append(discord.ui.Separator())
        buttons=[yes,no]
        if proof_url and not is_image:
            buttons.append(discord.ui.Button(label="Abrir comprovante",style=discord.ButtonStyle.link,url=proof_url,emoji="📎"))
        items.append(discord.ui.ActionRow(*buttons))
        items.append(discord.ui.TextDisplay("-# Sistema de LOGS • A decisão exige conferência humana"))
        self.add_item(discord.ui.Container(*items,accent_colour=0x9167FF))
    async def interaction_check(self,i):
        if not i.guild or not logs_staff_member(i.user):
            await i.response.send_message("Somente administradores, o cargo autorizado ou o usuário autorizado podem revisar.",ephemeral=True); return False
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
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(message),accent_colour=0x9167FF))
class MemberDecisionView(discord.ui.LayoutView):
    def __init__(self,rid,heading=None,detail=None):
        super().__init__(timeout=None)
        button=discord.ui.Button(label="Responder à decisão",style=discord.ButtonStyle.primary,custom_id=f"logs:reply:{rid}")
        async def callback(i): await i.response.send_modal(MemberReplyModal(rid))
        button.callback=callback
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(heading or f"## Resultado do cadastro #{rid}"),discord.ui.Separator(),discord.ui.TextDisplay(detail or "Use o botão abaixo para responder."),discord.ui.ActionRow(button),accent_colour=0x9167FF))
def transfer_record(tid):
    with db() as c:
        return c.execute("SELECT * FROM log_transfers WHERE id=?",(tid,)).fetchone()

def finish_transfer(tid,actor):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        t=c.execute("SELECT * FROM log_transfers WHERE id=?",(tid,)).fetchone()
        if not t: return False,"Transferência não encontrada."
        if t["status"]!="PENDENTE": return False,"Essa transferência já foi finalizada."
        if not t["sender_accepted"] or not t["receiver_accepted"]:
            return False,"As duas pessoas precisam aceitar antes da aprovação."
        try:
            movement(c,t["sender_id"],-t["amount"],"TRANSFERENCIA_ENVIADA",f"Transferência #{tid} enviada para <@{t['receiver_id']}>: {t['reason']}",actor,tid)
            movement(c,t["receiver_id"],t["amount"],"TRANSFERENCIA_RECEBIDA",f"Transferência #{tid} recebida de <@{t['sender_id']}>: {t['reason']}",actor,tid)
        except ValueError:
            return False,"O saldo de quem envia ficou insuficiente. A transferência não foi realizada."
        c.execute("UPDATE log_transfers SET status='APROVADA',reviewer_id=?,reviewed=? WHERE id=?",(actor,now(),tid))
    return True,"Transferência aprovada e LOGS movimentados com sucesso."

class TransferView(discord.ui.LayoutView):
    def __init__(self,tid):
        super().__init__(timeout=None)
        t=transfer_record(tid)
        if not t:
            self.add_item(discord.ui.Container(discord.ui.TextDisplay("Transferência não encontrada."),accent_colour=0xE74C3C))
            return
        sender=f"<@{t['sender_id']}>"; receiver=f"<@{t['receiver_id']}>"
        status={"PENDENTE":"⏳ Aguardando confirmações","APROVADA":"✅ Transferência aprovada","REPROVADA":"❌ Transferência recusada"}.get(t["status"],t["status"])
        checklist=f"⏳ Quem envia: {'aceitou' if t['sender_accepted'] else 'aguardando'}\n⏳ Quem recebe: {'aceitou' if t['receiver_accepted'] else 'aguardando'}\n🛡️ ADM: {'aprovado' if t['status']=='APROVADA' else 'pendente' if t['status']=='PENDENTE' else 'finalizado'}"
        items=[
            discord.ui.TextDisplay(f"### 🔹 LOGS › Transferência de saldo #{tid}\n\n💱 **{t['amount']:,} LOGS**\n\n{status}\n━━━━━━━━━━━━━━━━━━━━"),
            discord.ui.TextDisplay(f"{sender} ➡️ {receiver}\n\n💼 **Movimento**\n➡️ Envia: {sender}\n⬅️ Recebe: {receiver}\n\n💰 **Resumo antes de confirmar**\nSaldo de quem envia agora: **{balance(t['sender_id']):,} LOGS**\nSaldo após transferência: **{max(0,balance(t['sender_id'])-t['amount']):,} LOGS**\n\n⏳ **Checklist**\n{checklist}\n\n📝 **Motivo**\n{t['reason']}\n\n🛡️ **Solicitado por**\n{sender}\nID: {t['sender_id']}"),
        ]
        buttons=[]
        if t["status"]=="PENDENTE":
            buttons.append(discord.ui.Button(label="✅ Quem envia aceita",style=discord.ButtonStyle.secondary,custom_id=f"logs:transfer:sender:{tid}",disabled=bool(t["sender_accepted"])))
            buttons.append(discord.ui.Button(label="✅ Quem recebe aceita",style=discord.ButtonStyle.secondary,custom_id=f"logs:transfer:receiver:{tid}",disabled=bool(t["receiver_accepted"])))
            buttons.append(discord.ui.Button(label="🛡️ ADM aprovar",style=discord.ButtonStyle.success,custom_id=f"logs:transfer:approve:{tid}",disabled=not (t["sender_accepted"] and t["receiver_accepted"])))
            buttons.append(discord.ui.Button(label="❌ ADM reprovar",style=discord.ButtonStyle.danger,custom_id=f"logs:transfer:reject:{tid}"))
            for button in buttons:
                async def callback(i,b=button):
                    if b.custom_id.startswith("logs:transfer:sender:"):
                        if i.user.id!=t["sender_id"]: return await i.response.send_message("Somente quem envia pode confirmar.",ephemeral=True)
                        await self.accept(i,tid,"sender")
                    elif b.custom_id.startswith("logs:transfer:receiver:"):
                        if i.user.id!=t["receiver_id"]: return await i.response.send_message("Somente quem recebe pode confirmar.",ephemeral=True)
                        await self.accept(i,tid,"receiver")
                    elif b.custom_id.startswith("logs:transfer:approve:"):
                        if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Somente administradores podem aprovar.",ephemeral=True)
                        ok,msg=finish_transfer(tid,i.user.id)
                        if ok: await audit(i.guild,f"✅ Transferência #{tid} aprovada por {i.user.mention}: {t['amount']} LOGS de <@{t['sender_id']}> para <@{t['receiver_id']}>.")
                        await i.response.send_message(msg,ephemeral=True)
                        await i.message.edit(view=TransferView(tid))
                    else:
                        if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Somente administradores podem reprovar.",ephemeral=True)
                        with db() as c: c.execute("UPDATE log_transfers SET status='REPROVADA',reviewer_id=?,reviewed=? WHERE id=? AND status='PENDENTE'",(i.user.id,now(),tid))
                        await i.response.send_message("Transferência reprovada. Nenhum saldo foi alterado.",ephemeral=True)
                        await audit(i.guild,f"❌ Transferência #{tid} reprovada por {i.user.mention}.")
                        await i.message.edit(view=TransferView(tid))
                button.callback=callback
            items.append(discord.ui.ActionRow(*buttons))
        items.append(discord.ui.TextDisplay("-# Sistema de LOGS • Transferência sujeita à confirmação das duas pessoas e aprovação administrativa."))
        self.add_item(discord.ui.Container(*items,accent_colour=0x9167FF))
    async def accept(self,i,tid,who):
        with db() as c:
            t=c.execute("SELECT * FROM log_transfers WHERE id=?",(tid,)).fetchone()
            if not t or t["status"]!="PENDENTE": return await i.response.send_message("Esta transferência não está mais pendente.",ephemeral=True)
            col="sender_accepted" if who=="sender" else "receiver_accepted"
            c.execute(f"UPDATE log_transfers SET {col}=1 WHERE id=?",(tid,))
        await i.response.edit_message(view=TransferView(tid))

class Bot(commands.Bot):
    async def setup_hook(self):
        init_db()
        with db() as c:
            pending=c.execute("SELECT * FROM registrations WHERE status='PENDENTE'").fetchall()
            live_pending=c.execute("SELECT * FROM live_registrations WHERE status='PENDENTE'").fetchall()
            reviewed=[x["id"] for x in c.execute("SELECT id FROM registrations WHERE status IN ('APROVADO','REPROVADO')")]
        for r in pending:
            flags=json.loads(r["flags"])
            stats=f"👁️ **Views:** {r['views']:,}\n❤️ **Curtidas:** {r['likes']:,}\n👥 **Seguidores:** {r['followers']:,}"
            verification="\n".join(f"• {flag}" for flag in flags) if flags else "✅ Nenhuma duplicidade detectada.\nA conferência visual continua necessária."
            media_type=(r["proof_type"] or "").lower()
            image_ext=bool(r["proof_url"] and r["proof_url"].split("?",1)[0].lower().endswith((".png",".jpg",".jpeg",".gif",".webp")))
            is_image=bool(r["proof_url"] and (media_type.startswith("image/") or image_ext))
            self.add_view(ReviewView(r["id"],r["user_id"],r["roblox"],r["account"],stats,r["logs"],verification,r["proof_url"],is_image))
        for r in live_pending:
            flags=json.loads(r["flags"]); media=(r["proof_type"] or "").lower()
            ext=r["proof_url"].split("?",1)[0].lower().endswith((".png",".jpg",".jpeg",".gif",".webp"))
            self.add_view(LiveReviewView(r["id"],r["user_id"],r["roblox"],r["discord_nick"],r["comex_role"],r["views"],r["logs"],flags,r["proof_url"],media.startswith("image/") or ext))
        for rid in reviewed: self.add_view(MemberDecisionView(rid))
        with db() as c:
            transfer_ids=[r["id"] for r in c.execute("SELECT id FROM log_transfers WHERE status='PENDENTE'")]
        for tid in transfer_ids: self.add_view(TransferView(tid))
        synced=await self.tree.sync()
        log.info("Comandos globais sincronizados: %s. Disponíveis em todos os servidores onde o bot estiver instalado.",len(synced))
intents=discord.Intents.default()
bot=Bot(command_prefix="!",intents=intents)
@bot.event
async def on_ready(): log.info("Conectado como %s",bot.user)

@bot.tree.command(name="atualizar_conta",description="Configura as regras de conversão de LOGS.")
@admin()
async def atualizar_conta(i:discord.Interaction,views_unidade:app_commands.Range[int,1,100000000]=None,views_logs:app_commands.Range[int,0,1000000]=None,curtidas_unidade:app_commands.Range[int,1,100000000]=None,curtidas_logs:app_commands.Range[int,0,1000000]=None,seguidores_unidade:app_commands.Range[int,1,100000000]=None,seguidores_logs:app_commands.Range[int,0,1000000]=None):
    vals={"views_unit":views_unidade,"views_logs":views_logs,"likes_unit":curtidas_unidade,"likes_logs":curtidas_logs,"followers_unit":seguidores_unidade,"followers_logs":seguidores_logs}
    vals={k:v for k,v in vals.items() if v is not None}
    if not vals: return await i.response.send_message("Informe pelo menos um campo para atualizar. Os demais podem ficar vazios.",ephemeral=True)
    with db() as c:
        for k,v in vals.items(): c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(k,v))
    current=rules()
    summary=f"👁️ Views: {current['views_unit']} = {current['views_logs']} LOGS\n❤️ Curtidas: {current['likes_unit']} = {current['likes_logs']} LOGS\n👥 Seguidores: {current['followers_unit']} = {current['followers_logs']} LOGS"
    await i.response.send_message("✅ **Regras de vídeos atualizadas parcialmente.**\n"+summary,ephemeral=True)
    await audit(i.guild,f"⚙️ {i.user.mention} atualizou parcialmente as regras de conversão de vídeos.")
@bot.tree.command(name="editar_conta_live",description="Configura as regras de conversão das LIVES.")
@admin()
async def editar_conta_live(i:discord.Interaction,views_unidade:app_commands.Range[int,1,100000000]=None,views_logs:app_commands.Range[int,0,1000000]=None,curtidas_unidade:app_commands.Range[int,1,100000000]=None,curtidas_logs:app_commands.Range[int,0,1000000]=None):
    vals={"live_views_unit":views_unidade,"live_views_logs":views_logs,"live_likes_unit":curtidas_unidade,"live_likes_logs":curtidas_logs}
    vals={k:v for k,v in vals.items() if v is not None}
    if not vals: return await i.response.send_message("Informe pelo menos um campo para atualizar. Os demais podem ficar vazios.",ephemeral=True)
    with db() as c:
        for k,v in vals.items(): c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(k,v))
    current=rules()
    summary=f"👁️ Views: {current['live_views_unit']} = {current['live_views_logs']} LOGS\n❤️ Curtidas: {current['live_likes_unit']} = {current['live_likes_logs']} LOGS"
    await i.response.send_message("✅ **Regras de LIVES atualizadas parcialmente.**\n"+summary,ephemeral=True)
    await audit(i.guild,f"⚙️ {i.user.mention} atualizou parcialmente as regras de conversão LIVE.")
@bot.tree.command(name="registrar_logs_live",description="Envia uma LIVE para análise e cálculo de LOGS.")
async def registrar_logs_live(i:discord.Interaction,nickroblox:app_commands.Range[str,1,80],nickdiscord:app_commands.Range[str,1,80],cargonocomex:app_commands.Range[str,1,100],viewsdalive:app_commands.Range[int,0,100000000],comprovacao:discord.Attachment):
    if comprovacao.size>8*1024*1024: return await i.response.send_message("Comprovação maior que 8 MB.",ephemeral=True)
    if not (comprovacao.content_type or "").startswith(("image/","video/")): return await i.response.send_message("Envie uma imagem ou vídeo como comprovação.",ephemeral=True)
    try: raw=await comprovacao.read()
    except Exception: return await i.response.send_message("Não consegui ler a comprovação.",ephemeral=True)
    proof_hash=hashlib.sha256(raw).hexdigest()
    flags=[]
    total=calculate_live(viewsdalive)
    with db() as c:
        old=c.execute("SELECT live_id,user_id FROM live_proof_hashes WHERE hash=?",(proof_hash,)).fetchone()
        if old: flags.append(f"Comprovação idêntica já usada no cadastro LIVE #{old['live_id']} (usuário {old['user_id']}).")
        if not viewsdalive: flags.append("As visualizações estão zeradas.")
        cur=c.execute("INSERT INTO live_registrations(user_id,roblox,discord_nick,comex_role,views,likes,logs,proof_url,proof_hash,proof_type,flags,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(i.user.id,nickroblox,nickdiscord,cargonocomex,viewsdalive,0,total,comprovacao.url,proof_hash,comprovacao.content_type or "",json.dumps(flags,ensure_ascii=False),now()))
        rid=cur.lastrowid
        c.execute("INSERT OR IGNORE INTO live_proof_hashes VALUES(?,?,?,?)",(proof_hash,rid,i.user.id,now()))
    await i.response.send_message(f"Cadastro LIVE #{rid} enviado para análise. Cálculo preliminar: **{total} LOGS**. O crédito ocorre somente após aprovação.\n\n📅 Aviso: para as contagens semanais, só entram registros feitos nesta semana (segunda a domingo).",ephemeral=True)
    await post_live_review(rid)
    await post_registration_notice(i.guild,"live",rid)
    await audit(i.guild,f"🔴 Novo cadastro LIVE #{rid} enviado por {i.user.mention}.")
@bot.tree.command(name="ranking_semanal",description="Top 3 semanal de views e curtidas dos vídeos aprovados.")
async def ranking_semanal(i:discord.Interaction):
    from datetime import timedelta
    today=datetime.now(timezone.utc)
    monday=today.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=today.weekday())
    next_monday=monday+timedelta(days=7)
    with db() as c:
        views=c.execute("SELECT user_id,account,SUM(views) total FROM registrations WHERE status='APROVADO' AND created>=? AND created<? GROUP BY user_id,account ORDER BY total DESC LIMIT 3",(monday.isoformat(timespec="seconds"),next_monday.isoformat(timespec="seconds"))).fetchall()
        likes=c.execute("SELECT user_id,account,SUM(likes) total FROM registrations WHERE status='APROVADO' AND created>=? AND created<? GROUP BY user_id,account ORDER BY total DESC LIMIT 3",(monday.isoformat(timespec="seconds"),next_monday.isoformat(timespec="seconds"))).fetchall()
    def lines(rows):
        return "\n".join(f"**{n}.** <@{r['user_id']}> — **{r['total']:,}** ({r['account']})" for n,r in enumerate(rows,1)) or "Ainda não há vídeos aprovados nesta semana."
    layout=discord.ui.LayoutView(timeout=None)
    layout.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"# 🏆 RANKING SEMANAL • VÍDEOS\n-# Semana iniciada em {monday.strftime('%d/%m/%Y')} (UTC)"),
        discord.ui.Separator(),
        discord.ui.TextDisplay("## 👁️ Top 3 • Visualizações\n"+lines(views)),
        discord.ui.Separator(),
        discord.ui.TextDisplay("## ❤️ Top 3 • Curtidas\n"+lines(likes)),
        discord.ui.TextDisplay("-# ATENÇÃO: só contam registros feitos nesta semana (segunda a domingo). A regra vale para vídeos, LIVES e demais atividades; registros de semanas anteriores não entram nesta contagem."),
        accent_colour=0x9167FF))
    await i.response.send_message(view=layout)
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
    await i.response.send_message(f"Cadastro #{rid} enviado! Cálculo: {total} LOGS. Status: PENDENTE; o saldo só entra após aprovação.\n\n📅 Aviso: para as contagens semanais, só entram registros feitos nesta semana (segunda a domingo).",ephemeral=True)
    await post_review(bot,rid)
    await post_registration_notice(i.guild,"video",rid)
    await audit(i.guild,f"📥 Novo cadastro #{rid} enviado por {i.user.mention}.")
@bot.tree.command(name="transferir_logs",description="Solicita uma transferência de LOGS para outro membro.")
async def transferir_logs(i:discord.Interaction,recebedor:discord.Member,quantidade:app_commands.Range[int,1,1000000000],motivo:app_commands.Range[str,1,250]):
    if recebedor.bot: return await i.response.send_message("Não é possível transferir LOGS para bots.",ephemeral=True)
    if recebedor.id==i.user.id: return await i.response.send_message("Você não pode transferir LOGS para si mesmo.",ephemeral=True)
    saldo=balance(i.user.id)
    if quantidade>saldo: return await i.response.send_message(f"Saldo insuficiente. Sua carteira tem **{saldo:,} LOGS**.",ephemeral=True)
    with db() as c:
        cur=c.execute("INSERT INTO log_transfers(sender_id,receiver_id,amount,reason,created) VALUES(?,?,?,?,?)",(i.user.id,recebedor.id,quantidade,motivo,now()))
        tid=cur.lastrowid
    await i.response.send_message(f"🔄 Pedido de transferência: {i.user.mention} e {recebedor.mention} precisam aceitar. Depois um ADM aprova aqui mesmo.",view=TransferView(tid))
    await audit(i.guild,f"🔄 Nova transferência #{tid}: {i.user.mention} solicitou {quantidade} LOGS para {recebedor.mention}. Motivo: {motivo}")

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
@bot.tree.command(name="editar_saldo",description="Adiciona, remove ou define o saldo de um membro.")
@logs_staff()
@app_commands.choices(modo=[app_commands.Choice(name="Adicionar",value="adicionar"),app_commands.Choice(name="Remover",value="remover"),app_commands.Choice(name="Definir",value="definir")])
async def editar_saldo(i,membro:discord.Member,quantidade:app_commands.Range[int,0,1000000000],modo:app_commands.Choice[str],motivo:app_commands.Range[str,1,250]):
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            old=c.execute("SELECT balance FROM wallets WHERE user_id=?",(membro.id,)).fetchone(); before=old["balance"] if old else 0
            if modo.value=="definir":
                delta=quantidade-before; kind="SALDO_DEFINIDO"
            elif modo.value=="remover":
                delta=-quantidade; kind="SALDO_REMOVIDO"
            else:
                delta=quantidade; kind="SALDO_ADICIONADO"
            b=movement(c,membro.id,delta,kind,motivo,i.user.id)
    except ValueError as e:
        return await i.response.send_message(str(e),ephemeral=True)
    await i.response.send_message(f"Saldo de {membro.mention}: **{b} LOGS**.",ephemeral=True)
    await audit(i.guild,f"🛠️ {i.user.mention} usou o modo {modo.value} em {membro.mention}. Quantidade: {quantidade} LOGS. Motivo: {motivo}. Saldo final: {b} LOGS.")
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
    flags=json.loads(r["flags"])
    media_type=(r["proof_type"] or "").lower()
    image_ext=bool(r["proof_url"] and r["proof_url"].split("?",1)[0].lower().endswith((".png",".jpg",".jpeg",".gif",".webp")))
    is_image=bool(r["proof_url"] and (media_type.startswith("image/") or image_ext))
    items=[
        discord.ui.TextDisplay(f"# 🔎 Análise do cadastro #{cadastro_id}\n**Membro:** <@{r['user_id']}>\n**Roblox:** {r['roblox']}\n**Conta social:** {r['account']}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(f"### 📊 Estatísticas\n👁️ Views: **{r['views']:,}**\n❤️ Curtidas: **{r['likes']:,}**\n👥 Seguidores: **{r['followers']:,}**"),
        discord.ui.TextDisplay(f"### 💰 Resultado\n**{r['logs']} LOGS** • **Status:** {r['status']}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay("### 🛡️ Alertas\n"+("\n".join(f"• {flag}" for flag in flags) if flags else "✅ Nenhum alerta automático; confira visualmente."))
    ]
    if is_image: items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(r["proof_url"],description="Comprovante enviado")))
    if r["proof_url"] and not is_image:
        items.extend([discord.ui.Separator(),discord.ui.ActionRow(discord.ui.Button(label="Abrir comprovante",style=discord.ButtonStyle.link,url=r["proof_url"],emoji="📎"))])
    items.append(discord.ui.TextDisplay("-# Sistema de LOGS • Análise administrativa"))
    layout=discord.ui.LayoutView(timeout=None)
    layout.add_item(discord.ui.Container(*items,accent_colour=0x9167FF))
    await i.response.send_message(view=layout,ephemeral=True)
@bot.tree.command(name="aprovar_logs",description="Aprova cadastro e credita a carteira.")
@logs_staff()
async def aprovar_logs(i,cadastro_id:app_commands.Range[int,1,2147483647],motivo:app_commands.Range[str,3,500]):
    ok,msg,uid,logs=approve(cadastro_id,i.user.id,motivo); await i.response.send_message(msg,ephemeral=True)
    if ok:
        await update_registration_notice("video",cadastro_id,"APROVADO",motivo,i.user.id)
        await notify_member(bot,cadastro_id,True,motivo,logs)
        await audit(i.guild,f"✅ {i.user.mention} aprovou cadastro #{cadastro_id}. Motivo: {motivo}")
@bot.tree.command(name="reprovar_logs",description="Reprova cadastro com motivo.")
@logs_staff()
async def reprovar_logs(i,cadastro_id:app_commands.Range[int,1,2147483647],motivo:app_commands.Range[str,3,500]):
    ok,msg,uid,logs=reject(cadastro_id,i.user.id,motivo); await i.response.send_message(msg,ephemeral=True)
    if ok:
        await update_registration_notice("video",cadastro_id,"REPROVADO",motivo,i.user.id)
        await notify_member(bot,cadastro_id,False,motivo,logs)
        await audit(i.guild,f"❌ {i.user.mention} reprovou cadastro #{cadastro_id}: {motivo}")
if not TOKEN: raise RuntimeError("Configure DISCORD_TOKEN nas variáveis de ambiente.")
bot.run(TOKEN)
