import os
import sqlite3
import io
import datetime
import urllib.request
import pytz
import re
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# 0. CONFIGURAÇÕES GERAIS E CACHE
# ==========================================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
FUSO_HORARIO = pytz.timezone("America/Sao_Paulo")

FONT_PATH = "assets/fonts/Inter_24pt-Bold.ttf"

# Dicionário temporário para guardar o nome que o usuário deseja colocar antes de confirmar
pending_names = {}


def get_font(tamanho):
    try:
        return ImageFont.truetype(FONT_PATH, tamanho)
    except Exception:
        return ImageFont.load_default()


def limpar_texto(texto):
    if not texto:
        return "Usuário"
    texto_limpo = "".join(c for c in texto if ord(c) < 1000).strip()
    return texto_limpo if texto_limpo else "Usuário"


def is_valid_hex(hex_str):
    """Verifica se a string é um código HEX de cor válido"""
    return re.match(r"^#(?:[0-9a-fA-F]{3}){1,2}$", hex_str) is not None


# Cores Tema "Tokyo Night" (Padrões)
C_BG = "#1A1B26"
C_CARD = "#24283B"
C_TEXT = "#C0CAF5"
C_TEXT_MUTED = "#565F89"
C_GREEN = "#9ECE6A"  # Cor padrão, agora pode ser alterada pelo usuário
C_RED = "#F7768E"
C_EMPTY = "#414868"


# ==========================================
# 1. BANCO DE DADOS
# ==========================================
def init_db():
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS checks (user_id INTEGER, check_date TEXT, UNIQUE(user_id, check_date))"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS alertas (user_id INTEGER PRIMARY KEY, hora INTEGER, minuto INTEGER)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, nome TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS competicoes (user1 INTEGER, user2 INTEGER, status TEXT, start_date TEXT, UNIQUE(user1, user2))"""
    )

    # Atualizações de colunas caso o banco antigo já exista
    try:
        c.execute("ALTER TABLE competicoes ADD COLUMN start_date TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN nome_alterado INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute(f"ALTER TABLE users ADD COLUMN cor TEXT DEFAULT '{C_GREEN}'")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def registrar_usuario(user_id, nome_telegram):
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT nome, nome_alterado FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()

    if not row:
        # Usuário novo (salva a cor padrão)
        c.execute(
            "INSERT INTO users (user_id, nome, nome_alterado, cor) VALUES (?, ?, 0, ?)",
            (user_id, nome_telegram, C_GREEN),
        )
    else:
        # Só atualiza com o nome do Telegram se ele AINDA NÃO tiver usado o /setuser
        if row[1] == 0:
            c.execute(
                "UPDATE users SET nome = ? WHERE user_id = ?", (nome_telegram, user_id)
            )

    conn.commit()
    conn.close()


def obter_historico(user_id, dias):
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    hoje = datetime.date.today()
    datas_pesquisa = [
        (hoje - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(dias)
    ]
    data_mais_antiga = datas_pesquisa[-1]

    c.execute(
        "SELECT check_date FROM checks WHERE user_id = ? AND check_date >= ?",
        (user_id, data_mais_antiga),
    )
    dias_estudados = [row[0] for row in c.fetchall()]
    conn.close()
    return [d in dias_estudados for d in datas_pesquisa]


def calcular_stats(historico):
    pontos = sum(historico)
    dias_falhos = len(historico) - pontos
    ofensiva = 0
    for estudou in historico:
        if estudou:
            ofensiva += 1
        else:
            break
    return pontos, dias_falhos, ofensiva


def get_amigos(user_id):
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute(
        "SELECT user1, user2, start_date FROM competicoes WHERE (user1 = ? OR user2 = ?) AND status = 'aceito'",
        (user_id, user_id),
    )
    rows = c.fetchall()
    conn.close()
    amigos = []
    for r in rows:
        amigo = r[1] if r[0] == user_id else r[0]
        data_inicio = r[2] if r[2] else "Desconhecida"
        amigos.append({"id": amigo, "start": data_inicio})
    return sorted(amigos, key=lambda x: x["id"])


def get_nome(user_id):
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT nome FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else f"ID {user_id}"


def get_cor(user_id):
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT cor FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else C_GREEN


def get_dados_perfil(user_id):
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()

    c.execute("SELECT MIN(check_date) FROM checks WHERE user_id = ?", (user_id,))
    primeiro_check = c.fetchone()[0]
    if primeiro_check:
        primeiro_check = datetime.datetime.strptime(
            primeiro_check, "%Y-%m-%d"
        ).strftime("%d/%m/%Y")
    else:
        primeiro_check = "Nenhum"

    c.execute(
        "SELECT COUNT(*) FROM competicoes WHERE (user1=? OR user2=?) AND status='aceito'",
        (user_id, user_id),
    )
    total_amigos = c.fetchone()[0]

    c.execute(
        "SELECT SUBSTR(check_date, 1, 7) as mes, COUNT(*) FROM checks WHERE user_id = ? GROUP BY mes",
        (user_id,),
    )
    historico_bd = {row[0]: row[1] for row in c.fetchall()}
    conn.close()

    hoje = datetime.date.today()
    meses_grafico = []
    meses_nomes = [
        "Jan",
        "Fev",
        "Mar",
        "Abr",
        "Mai",
        "Jun",
        "Jul",
        "Ago",
        "Set",
        "Out",
        "Nov",
        "Dez",
    ]

    for i in range(5, -1, -1):
        m = hoje.month - i
        y = hoje.year
        while m < 1:
            m += 12
            y -= 1

        chave_bd = f"{y}-{m:02d}"
        nome_curto = f"{meses_nomes[m-1]}"
        valor = historico_bd.get(chave_bd, 0)
        meses_grafico.append((nome_curto, valor))

    return total_amigos, primeiro_check, meses_grafico


# ==========================================
# 2. GERAÇÃO DE IMAGENS (DASHBOARDS)
# ==========================================
def preparar_avatar(foto_bytes):
    if not foto_bytes:
        img = Image.new("RGB", (150, 150), C_EMPTY)
    else:
        img = Image.open(io.BytesIO(foto_bytes)).convert("RGBA")
        img = img.resize((150, 150), Image.Resampling.LANCZOS)
    mask = Image.new("L", (150, 150), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, 150, 150), fill=255)
    result = Image.new("RGBA", (150, 150), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask=mask)
    return result


def desenhar_heatmap_horizontal(
    draw, x_start, y_start, historico, colunas_maximas, cor_tema
):
    tam, gap = 26, 8
    for i, estudou in enumerate(historico):
        col = i % colunas_maximas
        row = i // colunas_maximas
        x = x_start + (col * (tam + gap))
        y = y_start + (row * (tam + gap))
        cor = cor_tema if estudou else C_EMPTY
        draw.rounded_rectangle([x, y, x + tam, y + tam], radius=6, fill=cor)


def gerar_dashboard_perfil(dados):
    largura, altura = 700, 950
    img = Image.new("RGB", (largura, altura), color=C_BG)
    draw = ImageDraw.Draw(img)
    cor_tema = dados.get("cor", C_GREEN)

    font_titulo = get_font(42)
    font_nome = get_font(32)
    font_info = get_font(20)
    font_info_bold = get_font(28)
    font_grafico_valor = get_font(18)
    font_grafico_mes = get_font(20)

    titulo = "SEU PERFIL"
    bbox = draw.textbbox((0, 0), titulo, font=font_titulo)
    draw.text(((largura - bbox[2]) / 2, 40), titulo, fill=C_TEXT, font=font_titulo)

    draw.rounded_rectangle([50, 120, 650, 890], radius=32, fill=C_CARD)

    avatar = preparar_avatar(dados["avatar_bytes"])
    img.paste(avatar, (275, 170), avatar)

    nome_limpo = limpar_texto(dados["nome"])
    bbox = draw.textbbox((0, 0), nome_limpo, font=font_nome)
    draw.text(((largura - bbox[2]) / 2, 340), nome_limpo, fill=C_TEXT, font=font_nome)

    amigos_txt = str(dados["total_amigos"])
    inicio_txt = str(dados["primeiro_check"])

    draw.rounded_rectangle([100, 420, 330, 500], radius=16, fill=C_BG)
    draw.text((120, 435), "Rivais", fill=C_TEXT_MUTED, font=font_info)
    draw.text((120, 460), amigos_txt, fill=cor_tema, font=font_info_bold)

    draw.rounded_rectangle([370, 420, 600, 500], radius=16, fill=C_BG)
    draw.text((390, 435), "1º Check-in", fill=C_TEXT_MUTED, font=font_info)
    draw.text((390, 460), inicio_txt, fill=C_TEXT, font=font_info_bold)

    draw.text(
        (100, 550),
        "Progresso Mensal (Últimos 6 Meses)",
        fill=C_TEXT_MUTED,
        font=font_info,
    )

    bar_width = 40
    gap = 45
    x_offset = 120
    base_y = 820
    max_height = 200

    for mes, valor in dados["meses_grafico"]:
        altura_barra = int((valor / 31) * max_height) if valor > 0 else 5
        y_topo = base_y - altura_barra

        cor_barra = cor_tema if valor > 0 else C_EMPTY
        draw.rounded_rectangle(
            [x_offset, y_topo, x_offset + bar_width, base_y], radius=8, fill=cor_barra
        )

        val_str = str(valor)
        bbox_val = draw.textbbox((0, 0), val_str, font=font_grafico_valor)
        w_val = bbox_val[2] - bbox_val[0]
        draw.text(
            (x_offset + (bar_width - w_val) / 2, y_topo - 30),
            val_str,
            fill=C_TEXT,
            font=font_grafico_valor,
        )

        bbox_mes = draw.textbbox((0, 0), mes, font=font_grafico_mes)
        w_mes = bbox_mes[2] - bbox_mes[0]
        draw.text(
            (x_offset + (bar_width - w_mes) / 2, base_y + 15),
            mes,
            fill=C_TEXT_MUTED,
            font=font_grafico_mes,
        )

        x_offset += bar_width + gap

    bio = io.BytesIO()
    img.save(bio, format="JPEG", quality=95)
    bio.seek(0)
    return bio


def gerar_dashboard_individual(dados, titulo_periodo):
    largura, altura = 700, 850
    img = Image.new("RGB", (largura, altura), color=C_BG)
    draw = ImageDraw.Draw(img)
    cor_tema = dados.get("cor", C_GREEN)

    font_titulo = get_font(42)
    font_nome = get_font(30)
    font_numero = get_font(96)
    font_label = get_font(26)
    font_stats = get_font(22)

    titulo_limpo = limpar_texto(titulo_periodo)
    bbox = draw.textbbox((0, 0), titulo_limpo, font=font_titulo)
    draw.text(
        ((largura - bbox[2]) / 2, 40), titulo_limpo, fill=C_TEXT, font=font_titulo
    )

    draw.rounded_rectangle([60, 120, 640, 800], radius=32, fill=C_CARD)

    avatar = preparar_avatar(dados["avatar_bytes"])
    img.paste(avatar, (int((largura - 150) / 2), 170), avatar)

    nome_limpo = limpar_texto(dados["nome"])
    bbox = draw.textbbox((0, 0), nome_limpo, font=font_nome)
    draw.text(((largura - bbox[2]) / 2, 340), nome_limpo, fill=C_TEXT, font=font_nome)

    pts, falhas, ofensiva = calcular_stats(dados["historico"])
    numero = str(pts)
    bbox = draw.textbbox((0, 0), numero, font=font_numero)
    draw.text(((largura - bbox[2]) / 2, 395), numero, fill=cor_tema, font=font_numero)

    label = "DIAS"
    bbox = draw.textbbox((0, 0), label, font=font_label)
    draw.text(((largura - bbox[2]) / 2, 510), label, fill=C_TEXT_MUTED, font=font_label)

    stats_text = f"Ofensiva: {ofensiva} dias   •   Falhas: {falhas}"
    bbox = draw.textbbox((0, 0), stats_text, font=font_stats)
    draw.text(
        ((largura - bbox[2]) / 2, 565), stats_text, fill=C_TEXT_MUTED, font=font_stats
    )

    total_dias = len(dados["historico"])
    colunas_por_linha = 7 if total_dias >= 7 else total_dias
    tam = 26
    gap = 8
    heatmap_width = (colunas_por_linha * tam) + ((colunas_por_linha - 1) * gap)
    x_start = int((largura - heatmap_width) / 2)
    y_start = 630

    desenhar_heatmap_horizontal(
        draw, x_start, y_start, dados["historico"], colunas_maximas=7, cor_tema=cor_tema
    )

    bio = io.BytesIO()
    img.save(bio, format="JPEG", quality=95)
    bio.seek(0)
    return bio


def gerar_dashboard_competicao(dados1, dados2):
    largura, altura = 1000, 720
    img = Image.new("RGBA", (largura, altura), color=C_BG)
    draw = ImageDraw.Draw(img)

    font_titulo = get_font(40)
    font_nome = get_font(28)
    font_vs = get_font(50)
    font_pontos = get_font(80)
    font_stats = get_font(20)
    font_small = get_font(16)

    titulo = "DUELO DE ESTUDOS"
    bbox = draw.textbbox((0, 0), titulo, font=font_titulo)
    draw.text(((largura - bbox[2]) / 2, 30), titulo, fill=C_TEXT, font=font_titulo)

    data_str = f"Início da disputa: {dados1['start_date']}"
    bbox = draw.textbbox((0, 0), data_str, font=font_small)
    draw.text(
        ((largura - bbox[2]) / 2, 80), data_str, fill=C_TEXT_MUTED, font=font_small
    )

    def desenhar_cartao(x_offset, dados):
        draw.rounded_rectangle(
            [x_offset, 130, x_offset + 380, 680], radius=20, fill=C_CARD
        )

        avatar = preparar_avatar(dados["avatar_bytes"])
        img.paste(avatar, (x_offset + 115, 160), avatar)

        nome_limpo = limpar_texto(dados["nome"])
        bbox = draw.textbbox((0, 0), nome_limpo, font=font_nome)
        draw.text(
            (x_offset + 190 - (bbox[2] / 2), 330),
            nome_limpo,
            fill=C_TEXT,
            font=font_nome,
        )

        pts, falhas, ofensiva = calcular_stats(dados["hist_14"])
        cor_tema = dados.get("cor", C_GREEN)

        pts_txt = f"{pts}"
        bbox = draw.textbbox((0, 0), pts_txt, font=font_pontos)
        draw.text(
            (x_offset + 190 - (bbox[2] / 2), 370),
            pts_txt,
            fill=cor_tema,
            font=font_pontos,
        )

        stats_txt = f"Ofensiva: {ofensiva} dias\nFalhas: {falhas}"
        draw.text((x_offset + 50, 480), stats_txt, fill=C_TEXT_MUTED, font=font_stats)
        return pts

    desenhar_cartao(70, dados1)
    desenhar_cartao(550, dados2)

    draw.text((465, 310), "VS", fill=C_RED, font=font_vs)

    tam_block, gap_block = 14, 4
    hm_width = (14 * tam_block) + (13 * gap_block)

    x_hm1 = 70 + int((380 - hm_width) / 2)
    draw.text(
        (x_hm1 + 15, 540),
        "Visão Geral (12 Semanas)",
        fill=C_TEXT_MUTED,
        font=font_small,
    )

    def desenhar_mini_heatmap(x_start, y_start, historico, cor_tema):
        for i, estudou in enumerate(reversed(historico)):
            col = i % 14
            row = i // 14
            x = x_start + (col * (tam_block + gap_block))
            y = y_start + (row * (tam_block + gap_block))
            cor = cor_tema if estudou else C_EMPTY
            draw.rounded_rectangle(
                [x, y, x + tam_block, y + tam_block], radius=3, fill=cor
            )

    desenhar_mini_heatmap(x_hm1, 570, dados1["hist_84"], dados1.get("cor", C_GREEN))

    x_hm2 = 550 + int((380 - hm_width) / 2)
    draw.text(
        (x_hm2 + 15, 540),
        "Visão Geral (12 Semanas)",
        fill=C_TEXT_MUTED,
        font=font_small,
    )
    desenhar_mini_heatmap(x_hm2, 570, dados2["hist_84"], dados2.get("cor", C_GREEN))

    bio = io.BytesIO()
    img.convert("RGB").save(bio, "JPEG", quality=95)
    bio.seek(0)
    return bio


# ==========================================
# 3. FUNÇÕES AUXILIARES DE TELEGRAM
# ==========================================
async def get_user_avatar(context, user_id):
    try:
        fotos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if fotos.total_count > 0:
            file = await context.bot.get_file(fotos.photos[0][-1].file_id)
            foto_bytes = await file.download_as_bytearray()
            return bytes(foto_bytes)
    except:
        pass
    return None


# ==========================================
# 4. COMANDOS DO BOT
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome = update.message.from_user.first_name
    registrar_usuario(user_id, nome)
    msg = (
        f"👋 <b>Olá, {nome}! Bem-vindo ao seu Checkpoint de Estudos.</b>\n\n"
        f"Eu estou aqui para te ajudar a manter a disciplina. "
        f"Comigo você pode registrar seus dias de estudo, ver gráficos do seu progresso "
        f"em quadradinhos e competir com seus amigos!\n\n"
        f"🆔 <b>Seu ID para passar aos amigos é:</b> <code>{user_id}</code>\n\n"
        f"👉 <b>Dica:</b> Toque no botão azul <b>Menu</b> ao lado da caixa de texto ou "
        f"digite <code>/</code> para ver todos os comandos."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome = update.message.from_user.first_name
    registrar_usuario(user_id, nome)
    hoje = datetime.date.today().strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect("estudos.db")
        c = conn.cursor()
        c.execute(
            "INSERT INTO checks (user_id, check_date) VALUES (?, ?)", (user_id, hoje)
        )
        conn.commit()
        conn.close()
        await update.message.reply_text("✅ Checkpoint salvo! Excelente trabalho hoje.")
    except sqlite3.IntegrityError:
        await update.message.reply_text("⚠️ Você já fez o check-in hoje!")


async def setuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name

    registrar_usuario(user_id, nome_telegram)

    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT nome_alterado FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()

    if row and row[0] == 1:
        return await update.message.reply_text(
            "❌ Você já personalizou seu nome. Só é permitido alterar uma vez!"
        )

    novo_nome = " ".join(context.args).strip()
    if not novo_nome:
        return await update.message.reply_text(
            "⚠️ Uso correto: `/setuser NOME DESEJADO`\nEx: `/setuser Kehlani`",
            parse_mode="Markdown",
        )

    if len(novo_nome) > 20:
        return await update.message.reply_text(
            "❌ O nome desejado é muito grande. Escolha um nome de até 20 caracteres."
        )

    pending_names[user_id] = novo_nome

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Sim, confirmar", callback_data=f"setname_yes_{user_id}"
            ),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"setname_no_{user_id}"),
        ]
    ]

    await update.message.reply_text(
        f"Você tem certeza que deseja alterar seu nome no perfil para <b>{novo_nome}</b>?\n\n"
        f"⚠️ <b>ATENÇÃO:</b> Esta ação só pode ser feita UMA VEZ.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


# ---- NOVO COMANDO: SETCOLOR ----
async def setcolor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name
    registrar_usuario(user_id, nome_telegram)

    if not context.args:
        return await update.message.reply_text(
            "🎨 <b>Como alterar sua cor:</b>\n\n"
            "Envie o comando junto com o código HEX da cor desejada.\n"
            "Exemplo: `/setcolor #FF5733` ou `/setcolor #00BFFF`\n\n"
            "Dica: Procure por 'Color Picker' no Google para achar um código.",
            parse_mode="HTML",
        )

    cor = context.args[0].upper()
    if not cor.startswith("#"):
        cor = "#" + cor

    if not is_valid_hex(cor):
        return await update.message.reply_text(
            "❌ Código Hexadecimal inválido. Certifique-se de usar o formato correto (Ex: #FF5733)."
        )

    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("UPDATE users SET cor = ? WHERE user_id = ?", (cor, user_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🎨 Sucesso! A cor de destaque do seu perfil foi alterada para <b>{cor}</b>.",
        parse_mode="HTML",
    )


async def perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name
    registrar_usuario(user_id, nome_telegram)

    nome_db = get_nome(user_id)
    cor_db = get_cor(user_id)

    msg_load = await update.message.reply_text(
        "📊 Analisando seu banco de dados e gerando Perfil..."
    )
    total_amigos, primeiro_check, meses_grafico = get_dados_perfil(user_id)

    dados = {
        "nome": nome_db,
        "cor": cor_db,
        "avatar_bytes": await get_user_avatar(context, user_id),
        "total_amigos": total_amigos,
        "primeiro_check": primeiro_check,
        "meses_grafico": meses_grafico,
    }

    img = gerar_dashboard_perfil(dados)
    await context.bot.send_photo(chat_id=update.message.chat_id, photo=img)
    await msg_load.delete()


async def semanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name
    registrar_usuario(user_id, nome_telegram)

    nome_db = get_nome(user_id)
    cor_db = get_cor(user_id)

    msg_load = await update.message.reply_text("📊 Gerando Relatório Semanal...")

    dados = {
        "nome": nome_db,
        "cor": cor_db,
        "avatar_bytes": await get_user_avatar(context, user_id),
        "historico": obter_historico(user_id, 7),
    }
    img = gerar_dashboard_individual(dados, "RELATÓRIO SEMANAL")
    await context.bot.send_photo(chat_id=update.message.chat_id, photo=img)
    await msg_load.delete()


async def mensal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name
    registrar_usuario(user_id, nome_telegram)

    nome_db = get_nome(user_id)
    cor_db = get_cor(user_id)

    msg_load = await update.message.reply_text("📊 Gerando Relatório Mensal...")

    dados = {
        "nome": nome_db,
        "cor": cor_db,
        "avatar_bytes": await get_user_avatar(context, user_id),
        "historico": obter_historico(user_id, 28),
    }
    img = gerar_dashboard_individual(dados, "RELATÓRIO MENSAL")
    await context.bot.send_photo(chat_id=update.message.chat_id, photo=img)
    await msg_load.delete()


async def alerta_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.chat_id
    await context.bot.send_message(
        chat_id=user_id,
        text="⏰ <b>Hora de estudar!</b>\nNão esqueça de mandar o /check hoje!",
        parse_mode="HTML",
    )


async def alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    try:
        h, m = map(int, context.args[0].split(":"))
        conn = sqlite3.connect("estudos.db")
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO alertas (user_id, hora, minuto) VALUES (?, ?, ?)",
            (user_id, h, m),
        )
        conn.commit()
        conn.close()

        for job in context.job_queue.get_jobs_by_name(str(user_id)):
            job.schedule_removal()
        t = datetime.time(hour=h, minute=m, tzinfo=FUSO_HORARIO)
        context.job_queue.run_daily(alerta_job, t, chat_id=user_id, name=str(user_id))
        await update.message.reply_text(f"✅ Alerta configurado para {h:02d}:{m:02d}!")
    except:
        await update.message.reply_text("Uso correto: /alerta HH:MM")


# ---- NOVO COMANDO: MEUS ALERTAS ----
async def meus_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT hora, minuto FROM alertas WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()

    if row:
        h, m = row
        await update.message.reply_text(
            f"⏰ Seu lembrete diário está ativo para as <b>{h:02d}:{m:02d}</b>.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "🚫 Você não possui nenhum alerta ativo. Use /alerta HH:MM para criar um."
        )


async def remover_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("DELETE FROM alertas WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    for job in context.job_queue.get_jobs_by_name(str(user_id)):
        job.schedule_removal()
    await update.message.reply_text("🚫 Alerta removido.")


async def convidar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome = update.message.from_user.first_name
    registrar_usuario(user_id, nome)
    nome_banco = get_nome(user_id)

    try:
        amigo_id = int(context.args[0])
        if amigo_id == user_id:
            return await update.message.reply_text("Você não pode convidar a si mesmo!")
        keyboard = [
            [
                InlineKeyboardButton("✅ Aceitar", callback_data=f"acc_{user_id}"),
                InlineKeyboardButton("❌ Recusar", callback_data=f"rej_{user_id}"),
            ]
        ]
        try:
            await context.bot.send_message(
                chat_id=amigo_id,
                text=f"⚔️ <b>Convite de Duelo!</b>\nO usuário {nome_banco} (ID: <code>{user_id}</code>) desafiou você. Aceita?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
            conn = sqlite3.connect("estudos.db")
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO competicoes (user1, user2, status) VALUES (?, ?, 'pendente')",
                (user_id, amigo_id),
            )
            conn.commit()
            conn.close()
            await update.message.reply_text(f"✅ Convite enviado!")
        except:
            await update.message.reply_text(
                "❌ Ele precisa iniciar o bot mandando um /start primeiro!"
            )
    except:
        await update.message.reply_text("Uso correto: /convidar ID_DO_AMIGO")


async def botao_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    meu_id = query.from_user.id
    data = query.data

    if data.startswith("setname_"):
        partes = data.split("_")
        resposta = partes[1]
        alvo_id = int(partes[2])

        if meu_id != alvo_id:
            return await context.bot.answer_callback_query(
                query.id, "Este botão não é para você!", show_alert=True
            )

        if resposta == "yes":
            novo_nome = pending_names.get(meu_id)
            if not novo_nome:
                return await query.edit_message_text(
                    "❌ Ocorreu um erro ou expirou. Tente o comando /setuser novamente."
                )

            conn = sqlite3.connect("estudos.db")
            c = conn.cursor()
            c.execute(
                "UPDATE users SET nome = ?, nome_alterado = 1 WHERE user_id = ?",
                (novo_nome, meu_id),
            )
            conn.commit()
            conn.close()

            pending_names.pop(meu_id, None)
            await query.edit_message_text(
                f"✅ Sucesso! Seu nome no perfil e nos relatórios agora será: <b>{novo_nome}</b>.",
                parse_mode="HTML",
            )

        elif resposta == "no":
            pending_names.pop(meu_id, None)
            await query.edit_message_text(
                "❌ Ação cancelada. Seu nome não foi alterado."
            )
        return

    acao, amigo_id_str = data.split("_")
    amigo_id = int(amigo_id_str)
    hoje = datetime.date.today().strftime("%d/%m/%Y")

    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    if acao == "acc":
        c.execute(
            "UPDATE competicoes SET status = 'aceito', start_date = ? WHERE user1 = ? AND user2 = ?",
            (hoje, amigo_id, meu_id),
        )
        conn.commit()
        await query.edit_message_text(f"✅ Duelo aceito! Use /amigos para ver a lista.")
        await context.bot.send_message(chat_id=amigo_id, text=f"🎉 Convite aceito!")
    elif acao == "rej":
        c.execute(
            "DELETE FROM competicoes WHERE user1 = ? AND user2 = ?", (amigo_id, meu_id)
        )
        conn.commit()
        await query.edit_message_text("❌ Você recusou o convite.")
    conn.close()


async def amigos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    lista_amigos = get_amigos(user_id)
    if not lista_amigos:
        return await update.message.reply_text("Você não tem rivais. Use /convidar ID")
    msg = "⚔️ <b>Sua Lista de Rivais:</b>\n\n"
    for index, amigo in enumerate(lista_amigos):
        nome = get_nome(amigo["id"])
        msg += f"<b>{index + 1}</b> - {nome} (ID: <code>{amigo['id']}</code>) - Início: {amigo['start']}\n"
    msg += "\nDigite: <code>/competicao NÚMERO</code>"
    await update.message.reply_text(msg, parse_mode="HTML")


async def competicao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    lista_amigos = get_amigos(user_id)
    if not lista_amigos:
        return await update.message.reply_text("Você não tem competições ativas.")
    try:
        num = int(context.args[0]) - 1
        amigo = lista_amigos[num]
        amigo_id = amigo["id"]
        start_date = amigo["start"]
    except:
        return await update.message.reply_text("Uso correto: /competicao NÚMERO")

    msg_load = await update.message.reply_text("📊 Gerando Dashboard de Competição...")

    dados1 = {
        "nome": get_nome(user_id),
        "cor": get_cor(user_id),
        "avatar_bytes": await get_user_avatar(context, user_id),
        "hist_14": obter_historico(user_id, 14),
        "hist_84": obter_historico(user_id, 84),
        "start_date": start_date,
    }
    dados2 = {
        "nome": get_nome(amigo_id),
        "cor": get_cor(amigo_id),
        "avatar_bytes": await get_user_avatar(context, amigo_id),
        "hist_14": obter_historico(amigo_id, 14),
        "hist_84": obter_historico(amigo_id, 84),
        "start_date": start_date,
    }

    img_bytes = gerar_dashboard_competicao(dados1, dados2)

    pts1, pts2 = sum(dados1["hist_14"]), sum(dados2["hist_14"])
    if pts1 > pts2:
        txt = "🏆 Você está ganhando nas últimas 2 semanas!"
    elif pts2 > pts1:
        txt = "💀 Você está perdendo! Reaja!"
    else:
        txt = "⚔️ Disputa acirrada! Estão empatados."

    await context.bot.send_photo(
        chat_id=update.message.chat_id, photo=img_bytes, caption=txt
    )
    await msg_load.delete()


# ==========================================
# 5. INICIALIZAÇÃO
# ==========================================
async def setup_comandos(application: Application):
    comandos = [
        BotCommand("start", "Mensagem de boas-vindas"),
        BotCommand("check", "Marca seu estudo de hoje"),
        BotCommand("perfil", "Seu Cartão de Perfil"),
        BotCommand("semanal", "Seu Cartão Semanal"),
        BotCommand("mensal", "Seu Cartão Mensal (28 dias)"),
        BotCommand("setuser", "Altera seu nome no card (1 uso)"),
        BotCommand("setcolor", "Altera a cor do perfil (Hex)"),
        BotCommand("alerta", "Ex: /alerta 08:30 (Cria lembrete)"),
        BotCommand("meus_alertas", "Vê seu alerta ativo"),
        BotCommand("remover_alerta", "Desativa lembrete"),
        BotCommand("convidar", "Convida amigo via ID"),
        BotCommand("amigos", "Lista de rivais"),
        BotCommand("competicao", "Ex: /competicao 1"),
    ]
    await application.bot.set_my_commands(comandos)

    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT user_id, hora, minuto FROM alertas")
    for row in c.fetchall():
        user_id, h, m = row
        t = datetime.time(hour=h, minute=m, tzinfo=FUSO_HORARIO)
        application.job_queue.run_daily(
            alerta_job, t, chat_id=user_id, name=str(user_id)
        )
    conn.close()


if __name__ == "__main__":
    init_db()
    app = Application.builder().token(TOKEN).post_init(setup_comandos).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("perfil", perfil))
    app.add_handler(CommandHandler("semanal", semanal))
    app.add_handler(CommandHandler("mensal", mensal))
    app.add_handler(CommandHandler("setuser", setuser))
    app.add_handler(CommandHandler("setcolor", setcolor))
    app.add_handler(CommandHandler("alerta", alerta))
    app.add_handler(CommandHandler("meus_alertas", meus_alertas))
    app.add_handler(CommandHandler("remover_alerta", remover_alerta))
    app.add_handler(CommandHandler("convidar", convidar))
    app.add_handler(CommandHandler("amigos", amigos))
    app.add_handler(CommandHandler("competicao", competicao))
    app.add_handler(CallbackQueryHandler(botao_callback))

    print("Bot rodando! Aperte Ctrl+C para parar.")
    app.run_polling()
