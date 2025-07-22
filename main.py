import os
import json
import re
import math
import sys
from pathlib import Path
from PIL import Image
import Levenshtein
import google.generativeai as genai
import discord
from discord.ext import commands
from dotenv import load_dotenv

# --- Загрузка и ВАЛИДАЦИЯ конфигурации ---
load_dotenv()


def load_and_validate_env():
    """Загружает и проверяет переменные окружения. Завершает работу при ошибке."""
    config = {
        'API_KEY': os.getenv('GOOGLE_AI_API_KEY'),
        'BOT_TOKEN': os.getenv('DISCORD_BOT_TOKEN'),
        'ADMIN_USER_IDS_STR': os.getenv('ADMIN_USER_IDS'),
        'LOG_CHANNEL_ID_STR': os.getenv('LOG_CHANNEL_ID'),
        'TARGET_CHANNEL_IDS_STR': os.getenv('TARGET_CHANNEL_IDS'),
        'MIN_GAMES_FOR_STATS_STR': os.getenv('MIN_GAMES_FOR_STATS')
    }
    if not all(config.values()):
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Не все переменные окружения заданы в файле .env!")
        print(
            "Убедитесь, что существуют: GOOGLE_AI_API_KEY, DISCORD_BOT_TOKEN, ADMIN_USER_IDS, LOG_CHANNEL_ID, TARGET_CHANNEL_IDS, MIN_GAMES_FOR_STATS"
        )
        sys.exit(1)
    try:
        config['ADMIN_IDS'] = [
            int(aid.strip()) for aid in config['ADMIN_USER_IDS_STR'].split(',')
        ]
        config['LOG_CHANNEL_ID'] = int(config['LOG_CHANNEL_ID_STR'])
        config['TARGET_CHANNEL_IDS'] = [
            int(cid.strip()) for cid in config['TARGET_CHANNEL_IDS_STR'].split(',')
        ]
        config['MIN_GAMES'] = int(config['MIN_GAMES_FOR_STATS_STR'])
    except (ValueError, TypeError):
        print(
            "❌ КРИТИЧЕСКАЯ ОШИБКА: ID или число игр в файле .env имеют неверный формат."
        )
        sys.exit(1)
    print("✅ Конфигурация успешно загружена и проверена.")
    return config


config = load_and_validate_env()

# --- Константы ---
MAX_DISTANCE = 3
RAW_STATS_FILE = 'raw_stats.json'
PLAYER_AVERAGES_FILE = 'player_averages.json'
IMAGES_FOLDER = 'images'
API_KEY = config.get('API_KEY')
BOT_TOKEN = config['BOT_TOKEN']
TARGET_CHANNEL_IDS = config['TARGET_CHANNEL_IDS']
ADMIN_IDS = config['ADMIN_IDS']
TARGET_EMOJI = "✅"
LOG_CHANNEL_ID = config['LOG_CHANNEL_ID']
MIN_GAMES = config['MIN_GAMES']

# --- Настройка Discord бота ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix='>', intents=intents)


# --- Функции обработки данных ---
def init_json_db(db_path: str, default_structure):
    if not os.path.exists(db_path):
        try:
            with open(db_path, 'w', encoding='utf-8') as f:
                json.dump(default_structure, f, ensure_ascii=False, indent=4)
        except IOError as e:
            print(f"Ошибка при создании файла '{db_path}': {e}")


def read_json_db(db_path: str):
    try:
        with open(db_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except IOError as e:
        print(f"Критическая ошибка при чтении файла '{db_path}': {e}")
        return None


def write_json_db(db_path: str, data):
    try:
        with open(db_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except IOError as e:
        print(f"Ошибка при записи в файл '{db_path}': {e}")


def extract_data_with_gemini(image_path: str, prompt: str) -> str:
    try:
        img = Image.open(image_path)
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([prompt, img])
        return response.text.strip()
    except Exception as e:
        return f"Ошибка: {e}"


def parse_and_store_data(image_name: str, data: str, raw_data_dict: dict):
    lines = data.strip().split('\n')
    stats_for_this_image = []
    for line in lines:
        match = re.match(
            r'^\s*(\d+)\s+([\w\s.-]+?)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)',
            line.strip())
        if match:
            place, nickname, kills, deaths, assists, treasury, score = match.groups()
            stats_for_this_image.append({
                "place": int(place),
                "nickname": nickname.strip(),
                "kills": int(kills),
                "deaths": int(deaths),
                "assists": int(assists),
                "treasury": int(treasury),
                "score": int(score)
            })
    if stats_for_this_image: raw_data_dict[image_name] = stats_for_this_image
    return len(stats_for_this_image)


def update_player_averages():
    raw_data_dict = read_json_db(RAW_STATS_FILE)
    if not raw_data_dict: return "Сырых данных для анализа нет."

    # --- НОВЫЙ, УЛУЧШЕННЫЙ БЛОК: Определение "активных" игроков по ID сообщения ---

    # 1. Получаем список всех имен файлов из нашей базы данных raw_stats.json.
    # Имя файла содержит ID сообщения: "1387832036580134933-image.png"
    all_known_files = list(raw_data_dict.keys())

    # 2. Сортируем этот список по ID сообщения, которое является числом в начале имени файла.
    # Превращаем ID в целое число (int) для корректной числовой, а не текстовой сортировки.
    all_known_files.sort(key=lambda filename: int(filename.split('-')[0]), reverse=True)

    # 3. Берем имена последних 10 файлов. Это и есть наши "недавние матчи".
    recent_files = set(all_known_files[:10])

    # 4. Собираем множество (set) никнеймов всех, кто играл в недавних матчах.
    active_nicknames = set()
    for filename in recent_files:
        # Эта проверка уже не обязательна, так как мы берем файлы из самого словаря,
        # но оставим ее для надежности.
        if filename in raw_data_dict:
            for player_stat in raw_data_dict[filename]:
                active_nicknames.add(player_stat['nickname'])

    # --- КОНЕЦ НОВОГО БЛОКА ---

    all_stats = [stat for sublist in raw_data_dict.values() for stat in sublist]
    if not all_stats: return "Сырые данные пусты, анализ невозможен."

    total_kills = sum(s.get('kills', 0) for s in all_stats)
    total_deaths = sum(s.get('deaths', 0) for s in all_stats)

    grouped_data = {}
    for player_stat in all_stats:
        nickname = player_stat['nickname']
        stats = (player_stat['place'], player_stat['kills'], player_stat['deaths'],
                 player_stat['assists'], player_stat['treasury'], player_stat['score'])
        found = False
        for name in grouped_data:
            if Levenshtein.distance(nickname, name) <= MAX_DISTANCE:
                grouped_data[name].append(stats)
                found = True
                break
        if not found: grouped_data[nickname] = [stats]

    new_averages = {}
    players_in_stats = 0
    for name, stats_list in grouped_data.items():
        is_active = False
        for active_nick in active_nicknames:
            if Levenshtein.distance(name, active_nick) <= MAX_DISTANCE:
                is_active = True
                break
        if not is_active:
            continue

        games = len(stats_list)
        if games < MIN_GAMES:
            continue

        cols = list(zip(*stats_list))
        avg_kills = sum(cols[1]) / games
        avg_deaths = sum(cols[2]) / games
        new_averages[name] = {
            "games_played": games,
            "avg_place": round(sum(cols[0]) / games),
            "kd": round(avg_kills / avg_deaths, 2) if avg_deaths > 0 else avg_kills,
            "avg_kills": round(avg_kills, 2),
            "avg_deaths": round(avg_deaths, 2),
            "avg_assists": round(sum(cols[3]) / games, 2),
            "avg_score": round(sum(cols[5]) / games, 2)
        }
        players_in_stats += 1

    write_json_db(PLAYER_AVERAGES_FILE, new_averages)

    summary_text = (
        f"Статистика успешно обновлена для {players_in_stats} игроков.\n"
        f"> (Требования: мин. **{MIN_GAMES}** игр и активность в **10** последних матчах)."
    )

    return {
        "summary_string": summary_text,
        "total_kills": total_kills,
        "total_deaths": total_deaths
    }


# --- Функции и классы Discord бота ---


def create_stats_embed(player_list, page_num, total_pages, sort_mode):
    """Эта функция создает Embed, автоматически разбивая игроков на несколько полей, если их много, и не использует видимых заголовков полей."""
    start_index = page_num * 10
    end_index = start_index + 10

    sort_descriptions = {
        'kd': "Рейтинг отсортирован по соотношению **K/D**.",
        'place': "Рейтинг отсортирован по **среднему месту** (от меньшего к большему)."
    }

    embed = discord.Embed(title="🏆 Статистика игроков",
                          description=sort_descriptions.get(sort_mode, "Статистика"),
                          color=0x3498db)

    # Невидимый символ (Zero-Width Space) для всех заголовков полей
    INVISIBLE_CHAR = "\u200b"

    players_on_this_page = player_list[start_index:end_index]

    if not players_on_this_page:
        embed.add_field(name=INVISIBLE_CHAR, value="Нет данных.", inline=False)
    else:
        current_field_value = ""
        for i, stats in enumerate(players_on_this_page, start=start_index + 1):
            nickname = stats.get('nickname', 'Неизвестный')
            kd = stats.get('kd', 0.0)
            games = stats.get('games_played', 0)
            avg_place = stats.get('avg_place', 0)
            avg_kills = stats.get('avg_kills', 0.0)
            avg_deaths = stats.get('avg_deaths', 0.0)

            player_string = (
                f"**{i}. {nickname}**\n"
                f"> K/D: `{kd:.2f}` | Ср. место: `{avg_place}` | Игр: `{games}`\n"
                f"> Ср. Убийства: `{round(avg_kills)}` | Ср. Смерти: `{round(avg_deaths)}`\n")

            # Проверяем, не превысит ли добавление новой строки лимит
            if len(current_field_value) + len(player_string) > 1024:
                # Если превысит, добавляем накопленное поле с невидимым заголовком
                embed.add_field(name=INVISIBLE_CHAR,
                                value=current_field_value,
                                inline=False)
                # И начинаем новое
                current_field_value = player_string
            else:
                # Иначе просто добавляем к текущему
                current_field_value += player_string

        # Добавляем последнее оставшееся поле с невидимым заголовком
        if current_field_value:
            embed.add_field(name=INVISIBLE_CHAR,
                            value=current_field_value,
                            inline=False)

    embed.set_footer(
        text=f"Страница {page_num + 1} / {total_pages} (мин. игр: {MIN_GAMES})")
    return embed


class StatsPaginationView(discord.ui.View):
    """Этот класс управляет кнопками и сортировкой под сообщением статистики."""

    def __init__(self, player_stats):
        super().__init__(timeout=180)
        self.all_player_stats = player_stats
        self.current_page = 0
        self.sort_by = 'kd'

        self._sort_stats()
        self.total_pages = math.ceil(len(self.all_player_stats) / 10)
        self.update_buttons_state()

    def _sort_stats(self):
        if self.sort_by == 'place':
            self.all_player_stats.sort(key=lambda p: p.get('avg_place', 999))
        else:
            self.all_player_stats.sort(key=lambda p: p.get('kd', 0), reverse=True)

    def update_buttons_state(self):
        self.sort_kd_button.disabled = self.sort_by == 'kd'
        self.sort_place_button.disabled = self.sort_by == 'place'
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    async def _update_view(self, interaction: discord.Interaction):
        self.update_buttons_state()
        embed = create_stats_embed(self.all_player_stats, self.current_page,
                                   self.total_pages, self.sort_by)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Сорт. по K/D", style=discord.ButtonStyle.success, row=0)
    async def sort_kd_button(self, interaction: discord.Interaction,
                             button: discord.ui.Button):
        if self.sort_by == 'kd':
            await interaction.response.defer()
            return
        self.sort_by = 'kd'
        self._sort_stats()
        self.current_page = 0
        await self._update_view(interaction)

    @discord.ui.button(label="Сорт. по Месту", style=discord.ButtonStyle.success, row=0)
    async def sort_place_button(self, interaction: discord.Interaction,
                                button: discord.ui.Button):
        if self.sort_by == 'place':
            await interaction.response.defer()
            return
        self.sort_by = 'place'
        self._sort_stats()
        self.current_page = 0
        await self._update_view(interaction)

    @discord.ui.button(label="◀ Назад", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(self, interaction: discord.Interaction,
                              button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await self._update_view(interaction)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Вперёд ▶", style=discord.ButtonStyle.primary, row=1)
    async def next_button(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            await self._update_view(interaction)
        else:
            await interaction.response.defer()


# --- События и команды бота ---
@bot.event
async def on_ready():
    os.makedirs(IMAGES_FOLDER, exist_ok=True)
    init_json_db(RAW_STATS_FILE, {})
    init_json_db(PLAYER_AVERAGES_FILE, {})
    if API_KEY:
        try:
            genai.configure(api_key=API_KEY)
            print("✅ Gemini API успешно сконфигурирован.")
        except Exception as e:
            print(f"❌ Ошибка конфигурации Gemini API: {e}")
    else:
        print("⚠️ Gemini API ключ не найден, команда >update не будет работать.")
    print(f'Бот {bot.user} запущен и готов к работе!')
    print(f"Администраторы бота: {ADMIN_IDS}")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not (payload.channel_id in TARGET_CHANNEL_IDS and payload.user_id in ADMIN_IDS
            and str(payload.emoji) == TARGET_EMOJI):
        return
    channel = bot.get_channel(payload.channel_id)
    if not channel: return
    try:
        message = await channel.fetch_message(payload.message_id)
        if not message.attachments: return
        for attachment in message.attachments:
            if "image" in attachment.content_type:
                file_path = os.path.join(IMAGES_FOLDER,
                                         f"{message.id}-{attachment.filename}")
                if not os.path.exists(file_path):
                    await attachment.save(file_path)
                    print(f"✅ Изображение '{file_path}' сохранено.")
    except Exception as e:
        print(f"❌ Ошибка при сохранении файла по реакции: {e}")


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if not (payload.channel_id in TARGET_CHANNEL_IDS and payload.user_id in ADMIN_IDS
            and str(payload.emoji) == TARGET_EMOJI):
        return
    channel = bot.get_channel(payload.channel_id)
    if not channel: return
    try:
        message = await channel.fetch_message(payload.message_id)
        if not message.attachments: return
        for attachment in message.attachments:
            file_path = os.path.join(IMAGES_FOLDER,
                                     f"{message.id}-{attachment.filename}")
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"🗑️ Изображение '{file_path}' удалено.")
    except Exception as e:
        print(f"❌ Ошибка при удалении файла по снятой реакции: {e}")


@bot.command()
@commands.check(lambda ctx: ctx.author.id in ADMIN_IDS)
async def update(ctx):
    """(Только для админа) Запускает обработку новых изображений и ПЕРЕСЧИТЫВАЕТ статистику."""
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await ctx.send("❌ **Ошибка конфигурации:** Не могу найти канал для логов.")
        return
    await ctx.send(f"✅ Команда принята. Результаты будут в {log_channel.mention}.")
    await log_channel.send(
        f"⏳ Начинаю полное обновление статистики по команде от {ctx.author.mention}...")

    image_dir = Path(IMAGES_FOLDER)
    image_files = list(image_dir.glob('*.png')) + list(image_dir.glob('*.jpg')) + list(
        image_dir.glob('*.jpeg'))
    raw_data = read_json_db(RAW_STATS_FILE)
    new_images = [f for f in image_files if f.name not in raw_data]

    processed_count = 0
    if new_images:
        await log_channel.send(
            f"🔍 Найдено **{len(new_images)}** новых изображений для обработки.")
        prompt = "Извлеки данные из таблицы на изображении. Каждая строка должна содержать: Место, Имя игрока, Убийства, Смерти, Помощь, Казна, Счет. Не включай заголовки таблицы в ответ. Разделяй значения пробелами."
        for image_path in new_images:
            print(f"--- Обрабатываю: {image_path.name} ---")
            extracted_data = await bot.loop.run_in_executor(None,
                                                            extract_data_with_gemini,
                                                            str(image_path), prompt)
            if not extracted_data.startswith("Ошибка"):
                parsed_count = parse_and_store_data(image_name=image_path.name,
                                                    data=extracted_data,
                                                    raw_data_dict=raw_data)
                print(f"Успешно распознано {parsed_count} строк.")
                processed_count += 1
            else:
                print(f"Ошибка от Gemini: {extracted_data}")
        if processed_count > 0:
            write_json_db(RAW_STATS_FILE, raw_data)
            await log_channel.send(
                f"✅ Обработано и сохранено в сырые данные: **{processed_count}** изображений."
            )
    else:
        await log_channel.send("ℹ️ Новых изображений для обработки не найдено.")

    print("Принудительный пересчет средних показателей...")
    await log_channel.send("🔄 Пересчитываю итоговую статистику игроков...")
    update_results = update_player_averages()

    final_message = []
    if isinstance(update_results, dict):
        final_message.append(f"🎉 **Обновление завершено!**")
        final_message.append(f"> {update_results['summary_string']}")
        final_message.append(
            f"⚔️ **Всего убийств по серверу:** `{update_results['total_kills']}`")
        final_message.append(
            f"💀 **Всего смертей по серверу:** `{update_results['total_deaths']}`")
    else:
        final_message.append(f"⚠️ **Завершено с предупреждением:**\n> {update_results}")
    await log_channel.send("\n".join(final_message))


@update.error
async def update_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ У вас нет прав для выполнения этой команды.")


@bot.command()
async def tab(ctx):
    """Отображает таблицу статистики игроков с кнопками сортировки."""
    player_averages = read_json_db(PLAYER_AVERAGES_FILE)
    if not player_averages:
        await ctx.send(
            f"Статистика пока пуста. Для попадания в рейтинг нужно сыграть минимум **{MIN_GAMES}** игр."
        )
        return
    player_list = [{'nickname': nk, **st} for nk, st in player_averages.items()]
    view = StatsPaginationView(player_list)
    initial_embed = create_stats_embed(view.all_player_stats, view.current_page,
                                       view.total_pages, view.sort_by)
    await ctx.send(embed=initial_embed, view=view)


# --- Запуск бота ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ Ошибка: Токен бота DISCORD_BOT_TOKEN не найден в файле .env")
    else:
        bot.run(BOT_TOKEN)
