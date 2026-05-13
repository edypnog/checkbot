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
    return re.match(r"^#(?:[0-9a-fA-F]{3}){1,2}$", hex_str) is not None


def hex_to_rgba(hex_color, alpha_percent):
    """Converte HEX para formato RGBA (Tupla) com base na porcentagem de opacidade"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c + c for c in hex_color)
    try:
        r, g, b = tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    except:
        r, g, b = (158, 206, 106)  # Fallback C_GREEN
    a = int((alpha_percent / 100.0) * 255)
    return (r, g, b, a)


# Cores Tema "Tokyo Night" (Padrões)
C_BG = "#1A1B26"
C_CARD = "#24283B"
C_TEXT = "#C0CAF5"
C_TEXT_MUTED = "#565F89"
C_GREEN = "#9ECE6A"
C_RED = "#F7768E"
C_EMPTY = "#414868"


# ==========================================
# 1. BANCO DE DADOS
# ==========================================
def init_db():
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()

    # 1. Tabela de Alertas, Users e Competições
    c.execute(
        """CREATE TABLE IF NOT EXISTS alertas (user_id INTEGER PRIMARY KEY, hora INTEGER, minuto INTEGER)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, nome TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS competicoes (user1 INTEGER, user2 INTEGER, status TEXT, start_date TEXT, UNIQUE(user1, user2))"""
    )

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

    # 2. Migração Segura da Tabela CHECKS para suportar TÓPICOS
    c.execute(
        "CREATE TABLE IF NOT EXISTS checks (user_id INTEGER, check_date TEXT, UNIQUE(user_id, check_date))"
    )
    c.execute("PRAGMA table_info(checks)")
    columns = [row[1] for row in c.fetchall()]

    if "topico" not in columns:
        # Se a tabela não tiver a coluna 'topico', recria a tabela para ajustar as constraints UNIQUE
        c.execute(
            """CREATE TABLE checks_new (user_id INTEGER, check_date TEXT, topico TEXT, UNIQUE(user_id, check_date, topico))"""
        )
        c.execute(
            """INSERT OR IGNORE INTO checks_new (user_id, check_date, topico) SELECT user_id, check_date, 'Geral' FROM checks"""
        )
        c.execute("DROP TABLE checks")
        c.execute("ALTER TABLE checks_new RENAME TO checks")

    conn.commit()
    conn.close()


def registrar_usuario(user_id, nome_telegram):
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT nome, nome_alterado FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute(
            "INSERT INTO users (user_id, nome, nome_alterado, cor) VALUES (?, ?, 0, ?)",
            (user_id, nome_telegram, C_GREEN),
        )
    else:
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

    # Retorna CONTAGEM de checks no dia
    c.execute(
        "SELECT check_date, COUNT(*) FROM checks WHERE user_id = ? AND check_date >= ? GROUP BY check_date",
        (user_id, data_mais_antiga),
    )
    contagens = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    return [contagens.get(d, 0) for d in datas_pesquisa]


def calcular_stats(historico):
    # historico é uma lista de inteiros: ex [0, 1, 3, 0]
    total_checks = sum(historico)
    dias_ativos = sum(1 for count in historico if count > 0)
    dias_falhos = len(historico) - dias_ativos

    ofensiva = 0
    for count in historico:
        if count > 0:
            ofensiva += 1
        else:
            break
    return total_checks, dias_falhos, ofensiva


def obter_topicos_populares(user_id, dias=None):
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    if dias:
        hoje = datetime.date.today()
        data_limite = (hoje - datetime.timedelta(days=dias)).strftime("%Y-%m-%d")
        c.execute(
            "SELECT topico, COUNT(*) FROM checks WHERE user_id = ? AND check_date >= ? GROUP BY topico ORDER BY COUNT(*) DESC LIMIT 3",
            (user_id, data_limite),
        )
    else:
        c.execute(
            "SELECT topico, COUNT(*) FROM checks WHERE user_id = ? GROUP BY topico ORDER BY COUNT(*) DESC LIMIT 3",
            (user_id,),
        )

    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Nenhum"
    return " • ".join([f"{row[0]} ({row[1]})" for row in rows])


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
    primeiro_check = (
        datetime.datetime.strptime(primeiro_check, "%Y-%m-%d").strftime("%d/%m/%Y")
        if primeiro_check
        else "Nenhum"
    )

    c.execute(
        "SELECT COUNT(*) FROM competicoes WHERE (user1=? OR user2=?) AND status='aceito'",
        (user_id, user_id),
    )
    total_amigos = c.fetchone()[0]

    c.execute(
        "SELECT SUBSTR(check_date, 1, 7) as mes, COUNT(DISTINCT check_date) FROM checks WHERE user_id = ? GROUP BY mes",
        (user_id,),
    )
    historico_bd = {row[0]: row[1] for row in c.fetchall()}
    conn.close()

    hoje = datetime.date.today()
    meses_grafico, meses_nomes = [], [
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
        meses_grafico.append((meses_nomes[m - 1], historico_bd.get(chave_bd, 0)))

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
    for i, count in enumerate(historico):
        col = i % colunas_maximas
        row = i // colunas_maximas
        x = x_start + (col * (tam + gap))
        y = y_start + (row * (tam + gap))

        # Opacidade com base nos checks diários
        if count == 0:
            cor = C_EMPTY
        elif count == 1:
            cor = hex_to_rgba(cor_tema, 40)
        elif count == 2:
            cor = hex_to_rgba(cor_tema, 70)
        else:  # 3+ checks
            cor = hex_to_rgba(cor_tema, 100)

        draw.rounded_rectangle([x, y, x + tam, y + tam], radius=6, fill=cor)


def gerar_dashboard_perfil(dados):
    # Base RGBA para aceitar transparência nos retângulos
    largura, altura = 700, 950
    img = Image.new("RGBA", (largura, altura), color=C_BG)
    draw = ImageDraw.Draw(img)
    cor_tema = dados.get("cor", C_GREEN)

    font_titulo, font_nome, font_info = get_font(42), get_font(32), get_font(20)
    font_info_bold, font_grafico_valor, font_grafico_mes = (
        get_font(28),
        get_font(18),
        get_font(20),
    )

    # Título, Card e Avatar
    draw.text(
        ((largura - draw.textbbox((0, 0), "SEU PERFIL", font=font_titulo)[2]) / 2, 40),
        "SEU PERFIL",
        fill=C_TEXT,
        font=font_titulo,
    )
    draw.rounded_rectangle([50, 120, 650, 890], radius=32, fill=C_CARD)
    avatar = preparar_avatar(dados["avatar_bytes"])
    img.paste(avatar, (275, 170), avatar)

    # Nome
    nome_limpo = limpar_texto(dados["nome"])
    draw.text(
        ((largura - draw.textbbox((0, 0), nome_limpo, font=font_nome)[2]) / 2, 340),
        nome_limpo,
        fill=C_TEXT,
        font=font_nome,
    )

    # Caixas (Rivais e 1º Check)
    draw.rounded_rectangle([100, 410, 330, 490], radius=16, fill=C_BG)
    draw.text((120, 425), "Rivais", fill=C_TEXT_MUTED, font=font_info)
    draw.text(
        (120, 450), str(dados["total_amigos"]), fill=cor_tema, font=font_info_bold
    )

    draw.rounded_rectangle([370, 410, 600, 490], radius=16, fill=C_BG)
    draw.text((390, 425), "1º Check-in", fill=C_TEXT_MUTED, font=font_info)
    draw.text(
        (390, 450), str(dados["primeiro_check"]), fill=C_TEXT, font=font_info_bold
    )

    # Tópicos Mais Estudados
    draw.text((100, 520), f"🔥 Focos: {dados['topicos']}", fill=C_TEXT, font=font_info)

    # Gráfico
    draw.text(
        (100, 560),
        "Dias Ativos Mensais (Últimos 6 Meses)",
        fill=C_TEXT_MUTED,
        font=font_info,
    )
    bar_width, gap, x_offset, base_y, max_height = 40, 45, 120, 830, 200
    for mes, valor in dados["meses_grafico"]:
        altura_barra = int((valor / 31) * max_height) if valor > 0 else 5
        y_topo = base_y - altura_barra
        cor_barra = hex_to_rgba(cor_tema, 100) if valor > 0 else C_EMPTY
        draw.rounded_rectangle(
            [x_offset, y_topo, x_offset + bar_width, base_y], radius=8, fill=cor_barra
        )

        w_val = draw.textbbox((0, 0), str(valor), font=font_grafico_valor)[2]
        draw.text(
            (x_offset + (bar_width - w_val) / 2, y_topo - 30),
            str(valor),
            fill=C_TEXT,
            font=font_grafico_valor,
        )

        w_mes = draw.textbbox((0, 0), mes, font=font_grafico_mes)[2]
        draw.text(
            (x_offset + (bar_width - w_mes) / 2, base_y + 15),
            mes,
            fill=C_TEXT_MUTED,
            font=font_grafico_mes,
        )

        x_offset += bar_width + gap

    bio = io.BytesIO()
    img.convert("RGB").save(bio, format="JPEG", quality=95)
    bio.seek(0)
    return bio


def gerar_dashboard_individual(dados, titulo_periodo):
    largura, altura = 700, 850
    img = Image.new("RGBA", (largura, altura), color=C_BG)
    draw = ImageDraw.Draw(img)
    cor_tema = dados.get("cor", C_GREEN)

    font_titulo, font_nome, font_numero = get_font(42), get_font(30), get_font(96)
    font_label, font_stats, font_topicos = get_font(26), get_font(22), get_font(18)

    titulo_limpo = limpar_texto(titulo_periodo)
    draw.text(
        ((largura - draw.textbbox((0, 0), titulo_limpo, font=font_titulo)[2]) / 2, 40),
        titulo_limpo,
        fill=C_TEXT,
        font=font_titulo,
    )

    draw.rounded_rectangle([60, 120, 640, 800], radius=32, fill=C_CARD)

    avatar = preparar_avatar(dados["avatar_bytes"])
    img.paste(avatar, (int((largura - 150) / 2), 170), avatar)

    nome_limpo = limpar_texto(dados["nome"])
    draw.text(
        ((largura - draw.textbbox((0, 0), nome_limpo, font=font_nome)[2]) / 2, 340),
        nome_limpo,
        fill=C_TEXT,
        font=font_nome,
    )

    pts, falhas, ofensiva = calcular_stats(dados["historico"])
    draw.text(
        ((largura - draw.textbbox((0, 0), str(pts), font=font_numero)[2]) / 2, 395),
        str(pts),
        fill=cor_tema,
        font=font_numero,
    )
    draw.text(
        ((largura - draw.textbbox((0, 0), "CHECKS", font=font_label)[2]) / 2, 510),
        "CHECKS",
        fill=C_TEXT_MUTED,
        font=font_label,
    )

    stats_text = f"Ofensiva: {ofensiva} dias   •   Falhas: {falhas}"
    draw.text(
        ((largura - draw.textbbox((0, 0), stats_text, font=font_stats)[2]) / 2, 560),
        stats_text,
        fill=C_TEXT_MUTED,
        font=font_stats,
    )

    topicos_text = f"Tópicos: {dados['topicos']}"
    draw.text(
        (
            (largura - draw.textbbox((0, 0), topicos_text, font=font_topicos)[2]) / 2,
            595,
        ),
        topicos_text,
        fill=C_TEXT,
        font=font_topicos,
    )

    total_dias = len(dados["historico"])
    col = 7 if total_dias >= 7 else total_dias
    hm_width = (col * 26) + ((col - 1) * 8)
    desenhar_heatmap_horizontal(
        draw,
        int((largura - hm_width) / 2),
        640,
        dados["historico"],
        colunas_maximas=7,
        cor_tema=cor_tema,
    )

    bio = io.BytesIO()
    img.convert("RGB").save(bio, format="JPEG", quality=95)
    bio.seek(0)
    return bio


def gerar_dashboard_competicao(dados1, dados2):
    largura, altura = 1000, 720
    img = Image.new("RGBA", (largura, altura), color=C_BG)
    draw = ImageDraw.Draw(img)

    font_titulo, font_nome, font_vs = get_font(40), get_font(28), get_font(50)
    font_pontos, font_stats, font_small = get_font(80), get_font(20), get_font(16)

    draw.text(
        (
            (largura - draw.textbbox((0, 0), "DUELO DE ESTUDOS", font=font_titulo)[2])
            / 2,
            30,
        ),
        "DUELO DE ESTUDOS",
        fill=C_TEXT,
        font=font_titulo,
    )
    data_str = f"Início da disputa: {dados1['start_date']}"
    draw.text(
        ((largura - draw.textbbox((0, 0), data_str, font=font_small)[2]) / 2, 80),
        data_str,
        fill=C_TEXT_MUTED,
        font=font_small,
    )

    def desenhar_cartao(x_offset, dados):
        draw.rounded_rectangle(
            [x_offset, 130, x_offset + 380, 680], radius=20, fill=C_CARD
        )
        avatar = preparar_avatar(dados["avatar_bytes"])
        img.paste(avatar, (x_offset + 115, 160), avatar)
        nome = limpar_texto(dados["nome"])
        draw.text(
            (
                x_offset + 190 - (draw.textbbox((0, 0), nome, font=font_nome)[2] / 2),
                330,
            ),
            nome,
            fill=C_TEXT,
            font=font_nome,
        )

        pts, falhas, ofensiva = calcular_stats(dados["hist_14"])
        cor_tema = dados.get("cor", C_GREEN)
        draw.text(
            (
                x_offset
                + 190
                - (draw.textbbox((0, 0), str(pts), font=font_pontos)[2] / 2),
                370,
            ),
            str(pts),
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

    def desenhar_mini(x_start, y_start, historico, cor_tema):
        for i, count in enumerate(reversed(historico)):
            col, row = i % 14, i // 14
            x, y = x_start + (col * (tam_block + gap_block)), y_start + (
                row * (tam_block + gap_block)
            )
            if count == 0:
                cor = C_EMPTY
            elif count == 1:
                cor = hex_to_rgba(cor_tema, 40)
            elif count == 2:
                cor = hex_to_rgba(cor_tema, 70)
            else:
                cor = hex_to_rgba(cor_tema, 100)
            draw.rounded_rectangle(
                [x, y, x + tam_block, y + tam_block], radius=3, fill=cor
            )

    x_hm1 = 70 + int((380 - hm_width) / 2)
    draw.text(
        (x_hm1 + 15, 540),
        "Visão Geral (12 Semanas)",
        fill=C_TEXT_MUTED,
        font=font_small,
    )
    desenhar_mini(x_hm1, 570, dados1["hist_84"], dados1.get("cor", C_GREEN))

    x_hm2 = 550 + int((380 - hm_width) / 2)
    draw.text(
        (x_hm2 + 15, 540),
        "Visão Geral (12 Semanas)",
        fill=C_TEXT_MUTED,
        font=font_small,
    )
    desenhar_mini(x_hm2, 570, dados2["hist_84"], dados2.get("cor", C_GREEN))

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
        f"Eu estou aqui para te ajudar a manter a disciplina.\n\n"
        f"👉 Use <code>/check Idiomas</code> para marcar estudo focado, ou apenas <code>/check</code> para estudo Geral.\n\n"
        f"🆔 <b>Seu ID para passar aos amigos é:</b> <code>{user_id}</code>\n"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome = update.message.from_user.first_name
    registrar_usuario(user_id, nome)
    hoje = datetime.date.today().strftime("%Y-%m-%d")

    # Extrai o tópico digitado pelo usuário. Se não digitar, é "Geral"
    if context.args:
        topico = " ".join(context.args).strip().title()[:25]  # Limita a 25 caracteres
    else:
        topico = "Geral"

    try:
        conn = sqlite3.connect("estudos.db")
        c = conn.cursor()
        c.execute(
            "INSERT INTO checks (user_id, check_date, topico) VALUES (?, ?, ?)",
            (user_id, hoje, topico),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Checkpoint de <b>{topico}</b> salvo! Excelente trabalho hoje.",
            parse_mode="HTML",
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(
            f"⚠️ Você já fez um check-in no tópico <b>{topico}</b> hoje!\nQuer marcar outro tópico? Use /check NomeDoTopico",
            parse_mode="HTML",
        )


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
            "⚠️ Uso correto: `/setuser NOME DESEJADO`", parse_mode="Markdown"
        )
    if len(novo_nome) > 20:
        return await update.message.reply_text(
            "❌ O nome desejado é muito grande. Escolha um nome de até 20 caracteres."
        )

    pending_names[user_id] = novo_nome
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Confirmar", callback_data=f"setname_yes_{user_id}"
            ),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"setname_no_{user_id}"),
        ]
    ]
    await update.message.reply_text(
        f"Tem certeza que deseja alterar seu nome no perfil para <b>{novo_nome}</b>?\nEsta ação só pode ser feita UMA VEZ.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def setcolor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name
    registrar_usuario(user_id, nome_telegram)

    if not context.args:
        return await update.message.reply_text(
            "🎨 Envie o comando com o HEX da cor. Ex: `/setcolor #FF5733`",
            parse_mode="Markdown",
        )

    cor = context.args[0].upper()
    if not cor.startswith("#"):
        cor = "#" + cor

    if not is_valid_hex(cor):
        return await update.message.reply_text("❌ Código HEX inválido.")

    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("UPDATE users SET cor = ? WHERE user_id = ?", (cor, user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"🎨 Cor do perfil alterada para <b>{cor}</b>.", parse_mode="HTML"
    )


async def perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name
    registrar_usuario(user_id, nome_telegram)

    msg_load = await update.message.reply_text("📊 Gerando Perfil...")
    total_amigos, primeiro_check, meses_grafico = get_dados_perfil(user_id)

    dados = {
        "nome": get_nome(user_id),
        "cor": get_cor(user_id),
        "avatar_bytes": await get_user_avatar(context, user_id),
        "total_amigos": total_amigos,
        "primeiro_check": primeiro_check,
        "meses_grafico": meses_grafico,
        "topicos": obter_topicos_populares(user_id),
    }

    img = gerar_dashboard_perfil(dados)
    await context.bot.send_photo(chat_id=update.message.chat_id, photo=img)
    await msg_load.delete()


async def semanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name
    registrar_usuario(user_id, nome_telegram)

    msg_load = await update.message.reply_text("📊 Gerando Relatório Semanal...")
    dados = {
        "nome": get_nome(user_id),
        "cor": get_cor(user_id),
        "avatar_bytes": await get_user_avatar(context, user_id),
        "historico": obter_historico(user_id, 7),
        "topicos": obter_topicos_populares(user_id, 7),
    }
    img = gerar_dashboard_individual(dados, "RELATÓRIO SEMANAL")
    await context.bot.send_photo(chat_id=update.message.chat_id, photo=img)
    await msg_load.delete()


async def mensal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    nome_telegram = update.message.from_user.first_name
    registrar_usuario(user_id, nome_telegram)

    msg_load = await update.message.reply_text("📊 Gerando Relatório Mensal...")
    dados = {
        "nome": get_nome(user_id),
        "cor": get_cor(user_id),
        "avatar_bytes": await get_user_avatar(context, user_id),
        "historico": obter_historico(user_id, 28),
        "topicos": obter_topicos_populares(user_id, 28),
    }
    img = gerar_dashboard_individual(dados, "RELATÓRIO MENSAL")
    await context.bot.send_photo(chat_id=update.message.chat_id, photo=img)
    await msg_load.delete()


# --- ALERTA E CONVIDAR CONTINUAM IDÊNTICOS ---
async def alerta_job(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text="⏰ <b>Hora de estudar!</b>\nNão esqueça do /check hoje!",
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


async def meus_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT hora, minuto FROM alertas WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        await update.message.reply_text(
            f"⏰ Lembrete ativo para as <b>{row[0]:02d}:{row[1]:02d}</b>.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("🚫 Sem alertas ativos.")


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
    registrar_usuario(user_id, update.message.from_user.first_name)
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
                text=f"⚔️ <b>Convite!</b>\n{get_nome(user_id)} desafiou você.",
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
            await update.message.reply_text("✅ Convite enviado!")
        except:
            await update.message.reply_text(
                "❌ Ele precisa iniciar o bot mandando um /start primeiro!"
            )
    except:
        await update.message.reply_text("Uso correto: /convidar ID_DO_AMIGO")


async def botao_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    meu_id, data = query.from_user.id, query.data

    if data.startswith("setname_"):
        res, alvo_id = data.split("_")[1], int(data.split("_")[2])
        if meu_id != alvo_id:
            return await context.bot.answer_callback_query(
                query.id, "Botão de outro usuário!", show_alert=True
            )
        if res == "yes":
            nome = pending_names.pop(meu_id, None)
            if not nome:
                return await query.edit_message_text("❌ Erro. Tente novamente.")
            conn = sqlite3.connect("estudos.db")
            c = conn.cursor()
            c.execute(
                "UPDATE users SET nome = ?, nome_alterado = 1 WHERE user_id = ?",
                (nome, meu_id),
            )
            conn.commit()
            conn.close()
            await query.edit_message_text(
                f"✅ Nome salvo: <b>{nome}</b>.", parse_mode="HTML"
            )
        else:
            pending_names.pop(meu_id, None)
            await query.edit_message_text("❌ Cancelado.")
        return

    acao, amigo_id = data.split("_")[0], int(data.split("_")[1])
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    if acao == "acc":
        c.execute(
            "UPDATE competicoes SET status = 'aceito', start_date = ? WHERE user1 = ? AND user2 = ?",
            (datetime.date.today().strftime("%d/%m/%Y"), amigo_id, meu_id),
        )
        await query.edit_message_text("✅ Duelo aceito!")
        await context.bot.send_message(chat_id=amigo_id, text="🎉 Convite aceito!")
    elif acao == "rej":
        c.execute(
            "DELETE FROM competicoes WHERE user1 = ? AND user2 = ?", (amigo_id, meu_id)
        )
        await query.edit_message_text("❌ Convite recusado.")
    conn.commit()
    conn.close()


async def amigos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    amigos_lista = get_amigos(user_id)
    if not amigos_lista:
        return await update.message.reply_text("Você não tem rivais. Use /convidar ID")
    msg = "⚔️ <b>Rivais:</b>\n\n"
    for idx, am in enumerate(amigos_lista):
        msg += f"<b>{idx + 1}</b> - {get_nome(am['id'])} (Início: {am['start']})\n"
    await update.message.reply_text(msg + "\nEx: `/competicao 1`", parse_mode="HTML")


async def competicao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    lista_amigos = get_amigos(user_id)
    if not lista_amigos:
        return await update.message.reply_text("Você não tem competições ativas.")
    try:
        amigo = lista_amigos[int(context.args[0]) - 1]
    except:
        return await update.message.reply_text("Uso correto: /competicao NÚMERO")

    msg_load = await update.message.reply_text("📊 Gerando Dashboard de Competição...")
    d1 = {
        "nome": get_nome(user_id),
        "cor": get_cor(user_id),
        "avatar_bytes": await get_user_avatar(context, user_id),
        "hist_14": obter_historico(user_id, 14),
        "hist_84": obter_historico(user_id, 84),
        "start_date": amigo["start"],
    }
    d2 = {
        "nome": get_nome(amigo["id"]),
        "cor": get_cor(amigo["id"]),
        "avatar_bytes": await get_user_avatar(context, amigo["id"]),
        "hist_14": obter_historico(amigo["id"], 14),
        "hist_84": obter_historico(amigo["id"], 84),
        "start_date": amigo["start"],
    }

    img_bytes = gerar_dashboard_competicao(d1, d2)
    p1, p2 = sum(d1["hist_14"]), sum(d2["hist_14"])
    await context.bot.send_photo(
        chat_id=update.message.chat_id,
        photo=img_bytes,
        caption=(
            "🏆 Você domina!"
            if p1 > p2
            else ("💀 Você perde!" if p2 > p1 else "⚔️ Empate!")
        ),
    )
    await msg_load.delete()


# ==========================================
# 5. INICIALIZAÇÃO
# ==========================================
async def setup_comandos(application: Application):
    comandos = [
        BotCommand("start", "Início"),
        BotCommand("check", "Ex: /check Inglês"),
        BotCommand("perfil", "Seu Cartão"),
        BotCommand("semanal", "Relatório 7 dias"),
        BotCommand("mensal", "Relatório 28 dias"),
        BotCommand("setuser", "Altera seu nome (1 uso)"),
        BotCommand("setcolor", "Altera a cor (Ex: #FF0000)"),
        BotCommand("alerta", "Ex: /alerta 08:30"),
        BotCommand("meus_alertas", "Vê o alerta ativo"),
        BotCommand("remover_alerta", "Desativa lembrete"),
        BotCommand("convidar", "Convida amigo via ID"),
        BotCommand("amigos", "Sua lista de rivais"),
        BotCommand("competicao", "Ver duelo ativo"),
    ]
    await application.bot.set_my_commands(comandos)
    conn = sqlite3.connect("estudos.db")
    c = conn.cursor()
    c.execute("SELECT user_id, hora, minuto FROM alertas")
    for r in c.fetchall():
        application.job_queue.run_daily(
            alerta_job,
            datetime.time(hour=r[1], minute=r[2], tzinfo=FUSO_HORARIO),
            chat_id=r[0],
            name=str(r[0]),
        )
    conn.close()


if __name__ == "__main__":
    init_db()
    app = Application.builder().token(TOKEN).post_init(setup_comandos).build()

    for cmd, handler in [
        ("start", start),
        ("check", check),
        ("perfil", perfil),
        ("semanal", semanal),
        ("mensal", mensal),
        ("setuser", setuser),
        ("setcolor", setcolor),
        ("alerta", alerta),
        ("meus_alertas", meus_alertas),
        ("remover_alerta", remover_alerta),
        ("convidar", convidar),
        ("amigos", amigos),
        ("competicao", competicao),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(CallbackQueryHandler(botao_callback))

    print("Bot rodando! Aperte Ctrl+C para parar.")
    app.run_polling()
