import os
import sqlite3
import asyncio
import threading
from datetime import datetime
from flask import Flask
import discord
from discord.ext import commands
from discord.ui import View, Button

# =============================================================
# 1. SERVIDOR WEB (Manter o bot ativo no Render)
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
            status TEXT
        )
    """)
    conn.commit()

    # Garante a coluna duracao_segundos sem quebrar bancos existentes
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

# Dicionário para gerenciar tarefas de fechamento automático por usuário
tarefas_fechamento = {}

# =============================================================
# 4. FUNÇÕES DE REGISTRO DE PONTO
# =============================================================
def iniciar_ponto(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Verifica se já existe um ponto aberto
    cursor.execute("SELECT id FROM registro_ponto WHERE user_id = ? AND status = 'ABERTO'", (user_id,))
    ponto_aberto = cursor.fetchone()
    
    if ponto_aberto:
        conn.close()
        return False, "Você já possui um ponto aberto!"
    
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO registro_ponto (user_id, inicio, status)
        VALUES (?, ?, 'ABERTO')
    """, (user_id, agora))
    conn.commit()
    conn.close()
    return True, f"Ponto iniciado com sucesso às **{agora}**!"

async def finalizar_ponto_usuario(member, motivo="Finalizado pelo usuário"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, inicio FROM registro_ponto WHERE user_id = ? AND status = 'ABERTO'", (member.id,))
    ponto = cursor.fetchone()
    
    if not ponto:
        conn.close()
        return False, "Nenhum ponto aberto encontrado."
    
    ponto_id, inicio_str = ponto
    inicio_dt = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S")
    agora_dt = datetime.now()
    agora_str = agora_dt.strftime("%Y-%m-%d %H:%M:%S")
    
    duracao_segundos = int((agora_dt - inicio_dt).total_seconds())
    horas, resto = divmod(duracao_segundos, 3600)
    minutos, segundos = divmod(resto, 60)
    tempo_formatado = f"{horas}h {minutos}m {segundos}s"
    
    cursor.execute("""
        UPDATE registro_ponto 
        SET fim = ?, status = 'FECHADO', duracao_segundos = ?
        WHERE id = ?
    """, (agora_str, duracao_segundos, ponto_id))
    conn.commit()
    conn.close()
    
    return True, f"Ponto finalizado! Duração: **{tempo_formatado}**. Motivo: {motivo}"

async def agendar_fechamento_automatico(member):
    try:
        await asyncio.sleep(180) # 3 minutos de tolerância após sair da call
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
# 5. PAINEL DE BOTÕES (UI COM RESPOSTA RÁPIDA / DEFER)
# =============================================================
class PontoView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Entrar em Serviço (Abrir Ponto)", style=discord.ButtonStyle.green, custom_id="btn_abrir_ponto")
    async def abrir_ponto_btn(self, interaction: discord.Interaction, button: Button):
        # Avisa ao Discord que o pedido foi recebido para evitar o erro de timeout
        await interaction.response.defer(ephemeral=True)
        
        sucesso, msg = iniciar_ponto(interaction.user.id)
        
        if interaction.user.id in tarefas_fechamento:
            tarefas_fechamento[interaction.user.id].cancel()
            tarefas_fechamento.pop(interaction.user.id, None)
            
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Sair de Serviço (Fechar Ponto)", style=discord.ButtonStyle.red, custom_id="btn_fechar_ponto")
    async def fechar_ponto_btn(self, interaction: discord.Interaction, button: Button):
        # Avisa ao Discord que o pedido foi recebido para evitar o erro de timeout
        await interaction.response.defer(ephemeral=True)
        
        sucesso, msg = await finalizar_ponto_usuario(interaction.user, motivo="Finalizado manualmente via painel")
        
        if interaction.user.id in tarefas_fechamento:
            tarefas_fechamento[interaction.user.id].cancel()
            tarefas_fechamento.pop(interaction.user.id, None)
            
        await interaction.followup.send(msg, ephemeral=True)

# =============================================================
# 6. EVENTOS E COMANDOS DO BOT
# =============================================================
@bot.event
async def on_ready():
    bot.add_view(PontoView())
    print(f"Bot PMESP Bate-Ponto conectado com sucesso como: {bot.user}")

@bot.command(name="setup_ponto")
@commands.has_permissions(administrator=True)
async def setup_ponto(ctx):
    embed = discord.Embed(
        title="🚔 **PMESP - SISTEMA DE BATE-PONTO** 🚔",
        description="Utilize os botões abaixo para registrar o início ou término do seu serviço.",
        color=discord.Color.blue()
    )
    embed.set_footer(text="Polícia Militar do Estado de São Paulo")
    await ctx.send(embed=embed, view=PontoView())

@bot.event
async def on_voice_state_update(member, before, after):
    # Quando o usuário sai do canal de voz
    if before.channel is not None and after.channel is None:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM registro_ponto WHERE user_id = ? AND status = 'ABERTO'", (member.id,))
        ponto = cursor.fetchone()
        conn.close()
        
        if ponto:
            # Agenda o fechamento automático após 3 minutos
            task = asyncio.create_task(agendar_fechamento_automatico(member))
            tarefas_fechamento[member.id] = task

    # Quando o usuário volta ao canal de voz antes dos 3 minutos
    elif before.channel is None and after.channel is not None:
        if member.id in tarefas_fechamento:
            tarefas_fechamento[member.id].cancel()
            tarefas_fechamento.pop(member.id, None)

# =============================================================
# 7. INICIALIZAÇÃO
# =============================================================
TOKEN = os.environ.get("DISCORD_TOKEN")

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("ERRO: A variável de ambiente DISCORD_TOKEN não foi configurada!")