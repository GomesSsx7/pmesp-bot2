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

# Configuração do Fuso Horário do Brasil
TZ_BR = ZoneInfo("America/Sao_Paulo")

def obter_agora_str():
    return datetime.now(TZ_BR).strftime("%H:%M:%S")

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
            duracao_segundos INTEGER
        )
    """)
    conn.commit()

    try:
        cursor.execute("ALTER TABLE registro_ponto ADD COLUMN duracao_segundos INTEGER")
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
# 4. FUNÇÕES DE REGISTRO DE PONTO
# =============================================================
def iniciar_ou_despausar_ponto(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, status FROM registro_ponto WHERE user_id = ? AND status IN ('ABERTO', 'PAUSADO')", (user_id,))
    ponto = cursor.fetchone()
    
    agora_hora = obter_agora_str()
    if ponto:
        if ponto[1] == 'PAUSADO':
            cursor.execute("UPDATE registro_ponto SET status = 'ABERTO' WHERE id = ?", (ponto[0],))
            conn.commit()
            conn.close()
            return True, f"🟢 **Ponto retomado** às **{agora_hora}**.", "REMANEJADO"
        conn.close()
        return False, "Você já possui um ponto aberto!", "ABERTO"
    
    agora_full = obter_agora_full_str()
    cursor.execute("INSERT INTO registro_ponto (user_id, inicio, status) VALUES (?, ?, 'ABERTO')", (user_id, agora_full))
    conn.commit()
    conn.close()
    return True, f"🟢 **Ponto iniciado** às **{agora_hora}**.", "NOVO"

def pausar_ponto(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, status FROM registro_ponto WHERE user_id = ? AND status IN ('ABERTO', 'PAUSADO')", (user_id,))
    ponto = cursor.fetchone()
    
    if not ponto:
        conn.close()
        return False, "Você não possui um ponto aberto para pausar.", "SEM_PONTO"
    
    if ponto[1] == 'PAUSADO':
        # Se clicar quando já está pausado -> Despausa
        cursor.execute("UPDATE registro_ponto SET status = 'ABERTO' WHERE id = ?", (ponto[0],))
        conn.commit()
        conn.close()
        agora_hora = obter_agora_str()
        return True, f"🟢 **Ponto retomado** às **{agora_hora}**.", "DESPAUSADO"
    else:
        # Pausa o ponto
        cursor.execute("UPDATE registro_ponto SET status = 'PAUSADO' WHERE id = ?", (ponto[0],))
        conn.commit()
        conn.close()
        return True, "🟡 **Ponto pausado**.", "PAUSADO"

async def finalizar_ponto_usuario(member, motivo="Finalizado pelo usuário"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, inicio FROM registro_ponto WHERE user_id = ? AND status IN ('ABERTO', 'PAUSADO')", (member.id,))
    ponto = cursor.fetchone()
    
    if not ponto:
        conn.close()
        return False, "Nenhum ponto aberto encontrado para finalizar."
    
    ponto_id, inicio_str = ponto
    inicio_dt = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_BR)
    agora_dt = datetime.now(TZ_BR)
    duracao_segundos = int((agora_dt - inicio_dt).total_seconds())
    
    horas, resto = divmod(duracao_segundos, 3600)
    minutos, segundos = divmod(resto, 60)
    tempo_formatado = f"{horas}h {minutos}m {segundos}s"
    
    cursor.execute("UPDATE registro_ponto SET fim = ?, status = 'FECHADO', duracao_segundos = ? WHERE id = ?", 
                   (agora_dt.strftime("%Y-%m-%d %H:%M:%S"), duracao_segundos, ponto_id))
    conn.commit()
    conn.close()
    
    return True, f"🔴 **Ponto finalizado!** Duração total: **{tempo_formatado}**. ({member.mention})"

def consultar_horas(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(duracao_segundos) FROM registro_ponto WHERE user_id = ? AND status = 'FECHADO'", (user_id,))
    total = cursor.fetchone()[0] or 0
    conn.close()
    
    horas, resto = divmod(total, 3600)
    minutos, _ = divmod(resto, 60)
    return f"📊 **Seu total de horas acumuladas:** {horas}h {minutos}m."

async def agendar_fechamento_automatico(member):
    try:
        await asyncio.sleep(180)
        sucesso, msg = await finalizar_ponto_usuario(member, motivo="Desconexão da call (>3 min)")
        if sucesso:
            try:
                await member.send(f"⚠️ O seu ponto foi finalizado automaticamente por ter saído da call há mais de 3 minutos.\n{msg}")
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        tarefas_fechamento.pop(member.id, None)

# =============================================================
# 5. PAINEL DE BOTÕES (DINÂMICO E SEM SPAM)
# =============================================================
class PontoView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Iniciar", style=discord.ButtonStyle.green, custom_id="btn_iniciar")
    async def btn_iniciar(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        sucesso, msg, estado = iniciar_ou_despausar_ponto(interaction.user.id)
        
        if interaction.user.id in tarefas_fechamento:
            tarefas_fechamento[interaction.user.id].cancel()
            tarefas_fechamento.pop(interaction.user.id, None)
            
        # Resposta privada para o policial (evita spam no canal)
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Pausar", style=discord.ButtonStyle.blurple, custom_id="btn_pausar")
    async def btn_pausar(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        sucesso, msg, novo_estado = pausar_ponto(interaction.user.id)
        
        # Resposta privada confirmando a pausa/retomada sem lotar o chat
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Finalizar", style=discord.ButtonStyle.red, custom_id="btn_finalizar")
    async def btn_finalizar(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        sucesso, msg = await finalizar_ponto_usuario(interaction.user)
        
        if interaction.user.id in tarefas_fechamento:
            tarefas_fechamento[interaction.user.id].cancel()
            tarefas_fechamento.pop(interaction.user.id, None)
            
        if sucesso:
            # Envia APENAS o resumo final no canal público de forma limpa
            await interaction.channel.send(msg)
            await interaction.followup.send("Seu expediente foi encerrado e enviado ao canal!", ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Horas", style=discord.ButtonStyle.gray, custom_id="btn_horas")
    async def btn_horas(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        msg = consultar_horas(interaction.user.id)
        await interaction.followup.send(msg, ephemeral=True)

# =============================================================
# 6. COMANDOS E EVENTOS
# =============================================================
@bot.event
async def on_ready():
    bot.add_view(PontoView())
    print(f"Bot PMESP Bate-Ponto conectado com sucesso como: {bot.user}")

@bot.command(name="setup_ponto")
@commands.has_permissions(administrator=True)
async def setup_ponto(ctx):
    embed = discord.Embed(
        title="🚔 **Bate-Ponto PMESP** 🚔",
        description="Clique nos botões abaixo para gerenciar o seu turno de patrulhamento:\n\n"
                    "🟢 **Iniciar:** Inicia/retoma a contagem do seu ponto.\n"
                    "🟡 **Pausar:** Coloca seu ponto em pausa ou retoma.\n"
                    "🔴 **Finalizar:** Encerra o seu expediente e publica o resumo.\n"
                    "📊 **Horas:** Consulta o seu total de horas acumuladas.",
        color=discord.Color.dark_grey()
    )
    embed.set_footer(text="PMESP Bate Ponto • Sistema Automático")
    await ctx.send(embed=embed, view=PontoView())

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is not None and after.channel is None:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM registro_ponto WHERE user_id = ? AND status IN ('ABERTO', 'PAUSADO')", (member.id,))
        ponto = cursor.fetchone()
        conn.close()
        if ponto:
            task = asyncio.create_task(agendar_fechamento_automatico(member))
            tarefas_fechamento[member.id] = task

    elif before.channel is None and after.channel is not None:
        if member.id in tarefas_fechamento:
            tarefas_fechamento[member.id].cancel()
            tarefas_fechamento.pop(member.id, None)

TOKEN = os.environ.get("DISCORD_TOKEN")
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)