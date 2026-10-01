import os
import sqlite3
import asyncio
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask
import discord
from discord.ext import commands
from discord.ui import View, Button

# Configuração do Fuso Horário do Brasil (Brasília)
TZ_BR = ZoneInfo("America/Sao_Paulo")

# Lista de IDs das calls permitidas para abrir ponto
CALLS_PERMITIDAS = [
    1551736045324730398,
    1551736077050581002,
    1551736102124265554,
    1553592199168532510,
    1553592245008076900,
    1553592276259836025,
    1551736815986155531
]

# Tempo mínimo em segundos (30 minutos = 1800 segundos)
TEMPO_MINIMO_SEGUNDOS = 1800

def obter_agora_dt():
    return datetime.now(TZ_BR)

def obter_agora_hora_str():
    return datetime.now(TZ_BR).strftime("%H:%M")

def obter_agora_full_str():
    return datetime.now(TZ_BR).strftime("%Y-%m-%d %H:%M:%S")

# =============================================================
# 1. SERVIDOR WEB (Render Free)
# =============================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot PMESP Bate-Ponto está Online!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()

keep_alive()

# =============================================================
# 2. BANCO DE DADOS (SQLite)
# =============================================================
DB_PATH = os.path.join(os.path.dirname(__file__), "ponto.db")

def setup_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS registro_ponto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            inicio TEXT,
            fim TEXT,
            status TEXT,
            duracao_segundos INTEGER,
            log_msg_id INTEGER,
            log_channel_id INTEGER,
            valido INTEGER DEFAULT 1
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config_servidor (
            guild_id INTEGER PRIMARY KEY,
            log_channel_id INTEGER
        )
    """)
    conn.commit()

    for col in ["duracao_segundos INTEGER", "log_msg_id INTEGER", "log_channel_id INTEGER", "valido INTEGER DEFAULT 1"]:
        try:
            cursor.execute(f"ALTER TABLE registro_ponto ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    conn.close()

setup_db()

# =============================================================
# 3. CONFIGURAÇÃO DO BOT
# =============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tarefas_fechamento = {}

# =============================================================
# 4. FUNÇÕES AUXILIARES DE FORMATAÇÃO E LOGS
# =============================================================
def obter_canal_log(guild_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT log_channel_id FROM config_servidor WHERE guild_id = ?", (guild_id,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else None

def salvar_canal_log(guild_id, channel_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO config_servidor (guild_id, log_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id
    """, (guild_id, channel_id))
    conn.commit()
    conn.close()

def formatar_tempo(duracao_segundos):
    horas, resto = divmod(duracao_segundos, 3600)
    minutos, _ = divmod(resto, 60)
    return f"{horas:02d}h {minutos:02d}m"

def formatar_tempo_ranking(duracao_segundos):
    horas, resto = divmod(duracao_segundos, 3600)
    minutos, _ = divmod(resto, 60)
    if horas > 0:
        return f"{horas}h {minutos}m"
    return f"{minutos}m"

def criar_embed_log(member: discord.Member, inicio_hora: str, fim_hora: str = "—", total_str: str = "Em andamento...", status: str = "ABERTO", e_valido: bool = True) -> discord.Embed:
    if status == "ABERTO":
        cor = discord.Color.blue()
        fim_hora = "—"
        total_str = "🕒 Em andamento..."
    else:  # FECHADO
        cor = discord.Color.blue() if e_valido else discord.Color.red()
        if not e_valido:
            total_str = f"⚠️ {total_str} (Inválido <30m)"

    embed = discord.Embed(
        title="🕒 Registro de Ponto",
        color=cor
    )
    
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="👤 Membro", 
        value=f"{member.mention}", 
        inline=False
    )
    
    embed.add_field(
        name="▶️ Início", 
        value=f"```\n{inicio_hora}\n```", 
        inline=True
    )
    
    embed.add_field(
        name="⏹️ Término", 
        value=f"```\n{fim_hora}\n```", 
        inline=True
    )
    
    embed.add_field(
        name=" Status", 
        value=f"```\n{total_str}\n```", 
        inline=False
    )

    embed.set_footer(text="</> Sistema desenvolvido por Gabriel Gomes")

    return embed

# =============================================================
# 5. LÓGICA DO PONTO
# =============================================================
async def processar_iniciar(interaction: discord.Interaction):
    user = interaction.user
    guild_id = interaction.guild.id
    
    if not user.voice or not user.voice.channel:
        return False, "❌ **Você precisa estar conectado em uma call autorizada para abrir o ponto!**"
        
    if user.voice.channel.id not in CALLS_PERMITIDAS:
        return False, "❌ **Você não está em uma call autorizada para abrir ponto!**"
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM registro_ponto WHERE user_id = ? AND status = 'ABERTO'", (user.id,))
    ponto = cursor.fetchone()
    
    if ponto:
        conn.close()
        return False, "Você já possui um ponto em andamento!"
    
    log_channel_id = obter_canal_log(guild_id)
    if not log_channel_id:
        conn.close()
        return False, "⚠️ O canal de logs ainda não foi configurado! Use `!set_log_channel #canal`."
    
    log_channel = interaction.guild.get_channel(log_channel_id)
    if not log_channel:
        conn.close()
        return False, "⚠️ Canal de logs não encontrado! Reconfigure com `!set_log_channel`."

    agora_full = obter_agora_full_str()
    agora_hora = obter_agora_hora_str()
    
    embed_log = criar_embed_log(user, agora_hora, status="ABERTO")
    msg_log = await log_channel.send(embed=embed_log)
    
    cursor.execute("""
        INSERT INTO registro_ponto (user_id, inicio, status, log_msg_id, log_channel_id)
        VALUES (?, ?, 'ABERTO', ?, ?)
    """, (user.id, agora_full, msg_log.id, log_channel.id))
    conn.commit()
    conn.close()
    
    return True, f"🟢 **Ponto iniciado!** Registro publicado em {log_channel.mention}."

async def processar_finalizar(member, guild, motivo="Finalizado pelo usuário"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, inicio, log_msg_id, log_channel_id FROM registro_ponto WHERE user_id = ? AND status = 'ABERTO'", (member.id,))
    ponto = cursor.fetchone()
    
    if not ponto:
        conn.close()
        return False, "Nenhum ponto aberto encontrado para finalizar."
        
    ponto_id, inicio_str, msg_id, channel_id = ponto
    inicio_dt = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_BR)
    agora_dt = obter_agora_dt()
    
    duracao_segundos = int((agora_dt - inicio_dt).total_seconds())
    tempo_fmt = formatar_tempo(duracao_segundos)
    
    inicio_hora = inicio_dt.strftime("%H:%M")
    fim_hora = agora_dt.strftime("%H:%M")
    
    e_valido = 1 if duracao_segundos >= TEMPO_MINIMO_SEGUNDOS else 0
    
    cursor.execute("""
        UPDATE registro_ponto 
        SET fim = ?, status = 'FECHADO', duracao_segundos = ?, valido = ?
        WHERE id = ?
    """, (agora_dt.strftime("%Y-%m-%d %H:%M:%S"), duracao_segundos, e_valido, ponto_id))
    conn.commit()
    conn.close()
    
    try:
        channel = guild.get_channel(channel_id)
        if channel:
            msg = await channel.fetch_message(msg_id)
            embed_log = criar_embed_log(member, inicio_hora, fim_hora, tempo_fmt, "FECHADO", e_valido=bool(e_valido))
            await msg.edit(embed=embed_log)
            
            emoji_reacao = "✅" if e_valido else "❌"
            await msg.add_reaction(emoji_reacao)
    except Exception:
        pass
        
    if e_valido:
        return True, f"🔴 **Ponto finalizado!** Duração: **{tempo_fmt}** (Contabilizado ✅)."
    else:
        return True, f"🔴 **Ponto finalizado!** Duração: **{tempo_fmt}**.\n⚠️ *Ponto não contabilizado por ter menos de 30 minutos (❌).* "

def consultar_horas(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(duracao_segundos) FROM registro_ponto WHERE user_id = ? AND status = 'FECHADO' AND valido = 1", (user_id,))
    total = cursor.fetchone()[0] or 0
    conn.close()
    
    return f"📊 **Total de horas acumuladas:** {formatar_tempo(total)}"

async def agendar_fechamento_automatico(member, guild):
    try:
        await asyncio.sleep(180)
        sucesso, msg = await processar_finalizar(member, guild, motivo="Desconexão da call (>3 min)")
        if sucesso:
            try:
                await member.send(f"⚠ O seu ponto foi finalizado automaticamente por ausência da call.\n{msg}")
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        tarefas_fechamento.pop(member.id, None)

# =============================================================
# 6. PAINEL DE BOTÕES (UI)
# =============================================================
class PontoView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ABRIR", style=discord.ButtonStyle.green, emoji="📝", custom_id="btn_iniciar")
    async def btn_iniciar(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        sucesso, msg = await processar_iniciar(interaction)
        
        if sucesso and interaction.user.id in tarefas_fechamento:
            tarefas_fechamento[interaction.user.id].cancel()
            tarefas_fechamento.pop(interaction.user.id, None)
            
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="FECHAR", style=discord.ButtonStyle.red, emoji="🔒", custom_id="btn_finalizar")
    async def btn_finalizar(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        sucesso, msg = await processar_finalizar(interaction.user, interaction.guild)
        
        if interaction.user.id in tarefas_fechamento:
            tarefas_fechamento[interaction.user.id].cancel()
            tarefas_fechamento.pop(interaction.user.id, None)
            
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="HORAS", style=discord.ButtonStyle.gray, emoji="🕒", custom_id="btn_horas")
    async def btn_horas(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        msg = consultar_horas(interaction.user.id)
        await interaction.followup.send(msg, ephemeral=True)

# =============================================================
# 7. COMANDOS E EVENTOS
# =============================================================
@bot.event
async def on_ready():
    bot.add_view(PontoView())
    print(f"Bot PMESP Bate-Ponto conectado com sucesso como: {bot.user}")

@bot.command(name="set_log_channel")
@commands.has_permissions(administrator=True)
async def set_log_channel(ctx, channel: discord.TextChannel):
    salvar_canal_log(ctx.guild.id, channel.id)
    await ctx.send(f"✅ Canal de registros de ponto definido para: {channel.mention}")

@bot.command(name="setup_ponto")
@commands.has_permissions(administrator=True)
async def setup_ponto(ctx):
    # Texto formatado numa única estrutura contínua para espaçamento perfeito
    descricao_texto = (
        "O bate-ponto é utilizado para contabilizar as horas de atividade de um membro no "
        "servidor. Cada ponto deverá possuir um acúmulo mínimo de **30 minutos** para ser "
        "registrado e contabilizado no banco de horas.\n\n"
        "ℹ️ **Funcionamento**\n\n"
        "1️⃣  Para iniciar um registro de ponto o membro deverá entrar em qualquer canal "
        "de voz da categoria **#PATRULHAMENTO PMESP** e clicar no botão \"ABRIR\" localizado abaixo.\n\n"
        "2️⃣  Para finalizar o registro, o membro deve permanecer no canal de voz e utilizar "
        "o botão \"FECHAR\" para que o ponto seja contabilizado. Caso o membro saia "
        "do canal de voz sem utilizar o comando o ponto é finalizado automaticamente após 3 minutos.\n\n"
        "3️⃣  Para verificar o total de horas registradas, basta acionar o botão \"HORAS\"."
    )

    embed = discord.Embed(
        title="🌐 | BATE PONTO PMESP",
        description=descricao_texto,
        color=discord.Color.blue()
    )
    
    embed.set_footer(text="</> Sistema desenvolvido por Gabriel Gomes")
    await ctx.send(embed=embed, view=PontoView())

@bot.command(name="ranking")
async def ranking(ctx):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT user_id, SUM(duracao_segundos) as total_segundos 
        FROM registro_ponto 
        WHERE status = 'FECHADO' AND valido = 1 AND duracao_segundos IS NOT NULL
        GROUP BY user_id 
        ORDER BY total_segundos DESC
    """)
    resultados = cursor.fetchall()
    conn.close()

    if not resultados:
        await ctx.send("ℹ️ Nenhum registro de ponto válido foi encontrado para gerar o ranking.")
        return

    embed = discord.Embed(
        title="🏆 Ranking de Tempo - Total",
        color=discord.Color.blue()
    )

    emojis_posicao = ["🥇", "🥈", "🥉"]
    linhas_ranking = []

    for idx, (user_id, total_segundos) in enumerate(resultados, start=1):
        tempo_str = formatar_tempo_ranking(total_segundos)
        
        if idx <= 3:
            pos_str = f"{emojis_posicao[idx-1]} **{idx}º**"
        else:
            pos_str = f"🏅 **{idx}º**"

        linhas_ranking.append(f"{pos_str} <@{user_id}>  -  **{tempo_str}**")

    embed.description = "\n".join(linhas_ranking)
    
    agora_hora = obter_agora_hora_str()
    embed.set_footer(text=f"Página 1/1 • Total: {len(resultados)} usuários • Hoje às {agora_hora} • Dev: Gabriel Gomes")

    await ctx.send(embed=embed)

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is not None and after.channel is None:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM registro_ponto WHERE user_id = ? AND status = 'ABERTO'", (member.id,))
        ponto = cursor.fetchone()
        conn.close()
        if ponto:
            task = asyncio.sleep(180) # 3 minutos
            tarefas_fechamento[member.id] = asyncio.create_task(agendar_fechamento_automatico(member, member.guild))

    elif before.channel is None and after.channel is not None:
        if member.id in tarefas_fechamento:
            tarefas_fechamento[member.id].cancel()
            tarefas_fechamento.pop(member.id, None)

TOKEN = os.environ.get("DISCORD_TOKEN")
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)