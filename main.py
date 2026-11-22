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
        CREATE TABLE IF NOT EXISTS registrations(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,roblox TEXT NOT NULL,account TEXT NOT NULL,views INTEGER NOT NULL,likes INTEGER NOT NULL,followers INTEGER NOT NULL,logs INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'PENDENTE',proof_url TEXT,proof_hash TEXT,flags TEXT NOT NULL DEFAULT '[]',reviewer INTEGER,reason TEXT,created TEXT NOT NULL,reviewed TEXT);
        CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,amount INTEGER NOT NULL,balance_after INTEGER NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,actor INTEGER NOT NULL,registration_id INTEGER,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proof_hashes(hash TEXT PRIMARY KEY,registration_id INTEGER NOT NULL,user_id INTEGER NOT NULL,created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS reg_user ON registrations(user_id,id);
        CREATE INDEX IF NOT EXISTS mov_user ON movements(user_id,id);
        """)
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
def approve(rid,actor):
    with db() as c:
        c.execute("BEGIN IMMEDIATE"); r=c.execute("SELECT * FROM registrations WHERE id=?",(rid,)).fetchone()
        if not r: return False,"Cadastro não encontrado."
        if r["status"]!="PENDENTE": return False,"Esse cadastro já foi analisado."
        b=movement(c,r["user_id"],r["logs"],"CADASTRO_APROVADO",f"Crédito do cadastro #{rid}",actor,rid)
        c.execute("UPDATE registrations SET status='APROVADO',reviewer=?,reviewed=? WHERE id=?",(actor,now(),rid))
    return True,f"Cadastro #{rid} aprovado: {r['logs']} LOGS creditados. Saldo: {b}."
def reject(rid,actor,reason):
    with db() as c:
        c.execute("BEGIN IMMEDIATE"); r=c.execute("SELECT status FROM registrations WHERE id=?",(rid,)).fetchone()
        if not r: return False,"Cadastro não encontrado."
        if r["status"]!="PENDENTE": return False,"Esse cadastro já foi analisado."
        c.execute("UPDATE registrations SET status='REPROVADO',reviewer=?,reason=?,reviewed=? WHERE id=?",(actor,reason,now(),rid))
    return True,f"Cadastro #{rid} reprovado. Motivo: {reason}"
async def audit(guild,msg):
    if AUDIT_ID and guild:
        ch=guild.get_channel(AUDIT_ID)
        if ch: await ch.send(msg[:1900])
async def post_review(bot,rid):
    with db() as c: r=c.execute("SELECT * FROM registrations WHERE id=?",(rid,)).fetchone()
    if not r or not REVIEW_ID: return
    ch=bot.get_channel(REVIEW_ID)
    if not ch:
        try: ch=await bot.fetch_channel(REVIEW_ID)
        except Exception: log.exception("Canal de análise indisponível"); return
    flags=json.loads(r["flags"])
    e=discord.Embed(title=f"Cadastro de LOGS #{rid}",color=discord.Color.orange())
    e.add_field(name="Membro",value=f"<@{r['user_id']}>",inline=False)
    e.add_field(name="Roblox / Conta",value=f"{r['roblox']} / {r['account']}",inline=False)
    e.add_field(name="Views / Curtidas / Seguidores",value=f"{r['views']} / {r['likes']} / {r['followers']}",inline=False)
    e.add_field(name="LOGS calculados",value=str(r["logs"]))
    e.add_field(name="Alertas automáticos",value="\\n".join(flags) if flags else "Nenhuma duplicidade detectada; confira visualmente.",inline=False)
    if r["proof_url"]: e.add_field(name="Comprovante",value=f"[Abrir comprovante]({r['proof_url']})",inline=False)
    e.set_footer(text="Alertas não provam fraude. A decisão exige conferência humana.")
    await ch.send(embed=e,view=ReviewView(rid))
class ReviewView(discord.ui.View):
    def __init__(self,rid):
        super().__init__(timeout=None); self.rid=rid
        self.children[0].custom_id=f"logs:approve:{rid}"
        self.children[1].custom_id=f"logs:reject:{rid}"
    async def interaction_check(self,i):
        if not i.guild or not i.user.guild_permissions.administrator:
            await i.response.send_message("Somente administradores podem revisar.",ephemeral=True); return False
        return True
    @discord.ui.button(label="Aprovar e creditar",style=discord.ButtonStyle.success)
    async def approve_button(self,i,b):
        ok,msg=approve(self.rid,i.user.id); await i.response.send_message(msg,ephemeral=True)
        if ok:
            for x in self.children: x.disabled=True
            await i.message.edit(view=self)
    @discord.ui.button(label="Reprovar",style=discord.ButtonStyle.danger)
    async def reject_button(self,i,b):
        ok,msg=reject(self.rid,i.user.id,"Reprovado pela equipe no painel.")
        await i.response.send_message(msg,ephemeral=True)
        if ok:
            for x in self.children: x.disabled=True
            await i.message.edit(view=self)
class Bot(commands.Bot):
    async def setup_hook(self):
        init_db()
        with db() as c: ids=[x["id"] for x in c.execute("SELECT id FROM registrations WHERE status='PENDENTE'")]
        for rid in ids: self.add_view(ReviewView(rid))
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
        try: proof_hash=hashlib.sha256(await comprovante.read()).hexdigest(); url=comprovante.url
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
        cur=c.execute("INSERT INTO registrations(user_id,roblox,account,views,likes,followers,logs,proof_url,proof_hash,flags,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(i.user.id,roblox_nick,conta_social,views,curtidas,seguidores,total,url,proof_hash,json.dumps(flags,ensure_ascii=False),now()))
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
async def aprovar_logs(i,cadastro_id:app_commands.Range[int,1,2147483647]):
    ok,msg=approve(cadastro_id,i.user.id); await i.response.send_message(msg,ephemeral=True)
    if ok: await audit(i.guild,f"✅ {i.user.mention} aprovou cadastro #{cadastro_id}.")
@bot.tree.command(name="reprovar_logs",description="Reprova cadastro com motivo.")
@admin()
async def reprovar_logs(i,cadastro_id:app_commands.Range[int,1,2147483647],motivo:app_commands.Range[str,1,250]):
    ok,msg=reject(cadastro_id,i.user.id,motivo); await i.response.send_message(msg,ephemeral=True)
    if ok: await audit(i.guild,f"❌ {i.user.mention} reprovou cadastro #{cadastro_id}: {motivo}")
if not TOKEN: raise RuntimeError("Configure DISCORD_TOKEN nas variáveis de ambiente.")
bot.run(TOKEN)
