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
            log_channel_id INTEGER
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config_servidor (
            guild_id INTEGER PRIMARY KEY,
            log_channel_id INTEGER
        )
    """)
    conn.commit()

    for col in ["duracao_segundos INTEGER", "log_msg_id INTEGER", "log_channel_id INTEGER"]:
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
    return f"{horas:02d}:{minutos:02d}"

def formatar_tempo_ranking(duracao_segundos):
    horas, resto = divmod(duracao_segundos, 3600)
    minutos, _ = divmod(resto, 60)
    if horas > 0:
        return f"{horas}h {minutos}m"
    return f"{minutos}m"

def gerar_texto_log_pequeno(member_mention, inicio_hora, fim_hora="EM AÇÃO", total_str="", status="ABERTO"):
    if status == "PAUSADO":
        fim_hora = "EM PAUSA"
        total_str = ""
    elif status == "ABERTO":
        fim_hora = "EM AÇÃO"
        total_str = ""

    log = (
        f"👤 **MEMBRO:** {member_mention}\n"
        f"➕ **INÍCIO:** {inicio_hora}\n"
        f"⤓ **TÉRMINO:** {fim_hora}\n"
        f"⏱️ **TOTAL:** {total_str}".strip()
    )
    return log

# =============================================================
# 5. LÓGICA DO PONTO
# =============================================================
async def processar_iniciar(interaction: discord.Interaction):
    user = interaction.user
    guild_id = interaction.guild.id
    
    # Validação de presença em call de voz autorizada
    if not user.voice or not user.voice.channel:
        return False, "❌ **Você precisa estar conectado em uma call autorizada para abrir o ponto!**"
        
    if user.voice.channel.id not in CALLS_PERMITIDAS:
        return False, "❌ **Você não está em uma call autorizada para abrir ponto!**"
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, status FROM registro_ponto WHERE user_id = ? AND status IN ('ABERTO', 'PAUSADO')", (user.id,))
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
    
    texto_log = gerar_texto_log_pequeno(user.mention, agora_hora, "EM AÇÃO", "", "ABERTO")
    msg_log = await log_channel.send(texto_log)
    
    cursor.execute("""
        INSERT INTO registro_ponto (user_id, inicio, status, log_msg_id, log_channel_id)
        VALUES (?, ?, 'ABERTO', ?, ?)
    """, (user.id, agora_full, msg_log.id, log_channel.id))
    conn.commit()
    conn.close()
    
    return True, f"🟢 **Ponto iniciado!** Registro publicado em {log_channel.mention}."

async def processar_pausar(interaction: discord.Interaction):
    user_id = interaction.user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, inicio, status, log_msg_id, log_channel_id FROM registro_ponto WHERE user_id = ? AND status IN ('ABERTO', 'PAUSADO')", (user_id,))
    ponto = cursor.fetchone()
    
    if not ponto:
        conn.close()
        return False, "Você não possui um ponto aberto para pausar/despausar."
        
    ponto_id, inicio_str, status_atual, msg_id, channel_id = ponto
    inicio_dt = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_BR)
    inicio_hora = inicio_dt.strftime("%H:%M")
    
    novo_status = "PAUSADO" if status_atual == "ABERTO" else "ABERTO"
    
    cursor.execute("UPDATE registro_ponto SET status = ? WHERE id = ?", (novo_status, ponto_id))
    conn.commit()
    conn.close()
    
    try:
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            msg = await channel.fetch_message(msg_id)
            texto_log = gerar_texto_log_pequeno(interaction.user.mention, inicio_hora, status=novo_status)
            await msg.edit(content=texto_log)
    except Exception:
        pass
        
    msg_retorno = "🟡 **Ponto pausado.**" if novo_status == "PAUSADO" else "🟢 **Ponto retomado.**"
    return True, msg_retorno

async def processar_finalizar(member, guild, motivo="Finalizado pelo usuário"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, inicio, log_msg_id, log_channel_id FROM registro_ponto WHERE user_id = ? AND status IN ('ABERTO', 'PAUSADO')", (member.id,))
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
    
    cursor.execute("""
        UPDATE registro_ponto 
        SET fim = ?, status = 'FECHADO', duracao_segundos = ? 
        WHERE id = ?
    """, (agora_dt.strftime("%Y-%m-%d %H:%M:%S"), duracao_segundos, ponto_id))
    conn.commit()
    conn.close()
    
    try:
        channel = guild.get_channel(channel_id)
        if channel:
            msg = await channel.fetch_message(msg_id)
            texto_log = gerar_texto_log_pequeno(member.mention, inicio_hora, fim_hora, tempo_fmt, "FECHADO")
            await msg.edit(content=texto_log)
    except Exception:
        pass
        
    return True, f"🔴 **Ponto finalizado!** Duração: **{tempo_fmt}**."

def consultar_horas(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(duracao_segundos) FROM registro_ponto WHERE user_id = ? AND status = 'FECHADO'", (user_id,))
    total = cursor.fetchone()[0] or 0
    conn.close()
    
    return f"📊 **Total de horas acumuladas:** {formatar_tempo(total)}"

async def agendar_fechamento_automatico(member, guild):
    try:
        await asyncio.sleep(180)
        sucesso, msg = await processar_finalizar(member, guild, motivo="Desconexão da call (>3 min)")
        if sucesso:
            try:
                await member.send(f"⚠️ O seu ponto foi finalizado automaticamente por ausência da call.\n{msg}")
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

    @discord.ui.button(label="Iniciar", style=discord.ButtonStyle.green, custom_id="btn_iniciar")
    async def btn_iniciar(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        sucesso, msg = await processar_iniciar(interaction)
        
        if sucesso and interaction.user.id in tarefas_fechamento:
            tarefas_fechamento[interaction.user.id].cancel()
            tarefas_fechamento.pop(interaction.user.id, None)
            
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Pausar / Despausar", style=discord.ButtonStyle.blurple, custom_id="btn_pausar")
    async def btn_pausar(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        sucesso, msg = await processar_pausar(interaction)
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Finalizar", style=discord.ButtonStyle.red, custom_id="btn_finalizar")
    async def btn_finalizar(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        sucesso, msg = await processar_finalizar(interaction.user, interaction.guild)
        
        if interaction.user.id in tarefas_fechamento:
            tarefas_fechamento[interaction.user.id].cancel()
            tarefas_fechamento.pop(interaction.user.id, None)
            
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Horas", style=discord.ButtonStyle.gray, custom_id="btn_horas")
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
    embed = discord.Embed(
        title="🚔 **Bate-Ponto PMESP** 🚔",
        description="Clique nos botões abaixo para gerenciar o seu turno de patrulhamento:\n\n"
                    "🟢 **Iniciar:** Inicia o seu ponto.\n"
                    "🟡 **Pausar / Despausar:** Alterna a pausa do seu ponto.\n"
                    "🔴 **Finalizar:** Encerra o seu expediente.\n"
                    "📊 **Horas:** Consulta o seu total acumulado.",
        color=discord.Color.dark_grey()
    )
    embed.set_footer(text="PMESP Bate Ponto • Sistema Automático")
    await ctx.send(embed=embed, view=PontoView())

@bot.command(name="ranking")
async def ranking(ctx):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT user_id, SUM(duracao_segundos) as total_segundos 
        FROM registro_ponto 
        WHERE status = 'FECHADO' AND duracao_segundos IS NOT NULL
        GROUP BY user_id 
        ORDER BY total_segundos DESC
    """)
    resultados = cursor.fetchall()
    conn.close()

    if not resultados:
        await ctx.send("ℹ️ Nenhum registro de ponto finalizado foi encontrado para gerar o ranking.")
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
    embed.set_footer(text=f"Página 1/1 • Total: {len(resultados)} usuários • Hoje às {agora_hora}")

    await ctx.send(embed=embed)

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is not None and after.channel is None:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM registro_ponto WHERE user_id = ? AND status IN ('ABERTO', 'PAUSADO')", (member.id,))
        ponto = cursor.fetchone()
        conn.close()
        if ponto:
            task = asyncio.create_task(agendar_fechamento_automatico(member, member.guild))
            tarefas_fechamento[member.id] = task

    elif before.channel is None and after.channel is not None:
        if member.id in tarefas_fechamento:
            tarefas_fechamento[member.id].cancel()
            tarefas_fechamento.pop(member.id, None)

TOKEN = os.environ.get("DISCORD_TOKEN")
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)