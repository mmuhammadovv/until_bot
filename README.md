# until_bot — бот обратного отсчёта

Телеграм-бот, который каждый день присылает, сколько дней осталось до дат, выбранных пользователем.

```
⏳ Обратный отсчёт

• 47 дней до «Новый год» (31.12.2026)
• 213 дней до «День рождения мамы» (15.06.2027)
```

## Возможности

- Добавить дату — просто написать боту `31.12.2026 Новый год` (или `/add …`). Год можно не указывать: `15.06 ДР` — ближайшее 15 июня.
- `/list` — список дат с кнопками удаления.
- `/now` — прислать отсчёт сразу.
- При первом `/start` бот кнопками спрашивает часовой пояс и время, когда каждый день присылать уведомление.
- `/time` — поменять время (кнопки по часам или точно: `/time 08:30`).
- `/tz` — поменять часовой пояс (кнопки с городами или вручную: `/tz Asia/Samarkand`, `/tz +5`).
- `/settings`, `/help`, `/stop` (удалить свои данные).
- В день события бот пишет «🎉 сегодня!», а на следующий день убирает его из списка.
- Если бот был выключен в момент отправки, сообщение уйдёт сразу после запуска, но не больше одного раза в день.

## Запуск

1. Создайте бота у [@BotFather](https://t.me/BotFather) и получите токен.
2. Установите зависимости (Python 3.10+):
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Скопируйте `.env.example` в `.env` и впишите `BOT_TOKEN`.
4. Запустите:
   ```bash
   python -m until_bot
   ```

Данные хранятся в SQLite-файле `until_bot.sqlite3` (путь меняется через `DB_PATH`).

## Работа онлайн 24/7 (для вас и друзей)

Бот многопользовательский: каждый, кто напишет ему `/start`, получает свой список дат,
своё время и часовой пояс. Нужно лишь, чтобы бот был постоянно запущен на сервере.
Подойдёт любой VPS (Linux, 512 МБ RAM хватит) — открывать порты и иметь домен не нужно,
бот сам опрашивает Telegram.

### Вариант 0 — бесплатный хостинг ботов без карты (панель управления)

1. Скачайте репозиторий архивом (GitHub → Code → Download ZIP) и распакуйте.
2. Загрузите в файловый менеджер панели: папку `until_bot/`, файлы `main.py` и `requirements.txt`.
3. Создайте там же файл `.env` с одной строкой `BOT_TOKEN=ваш_токен`
   (или задайте `BOT_TOKEN` в разделе переменных окружения / Startup).
4. Стартовый файл — `main.py`; зависимости ставятся из `requirements.txt`.
5. Нажмите Start. В консоли должно появиться `Start polling`.

### Вариант 1 — Docker (рекомендуется)

```bash
git clone <url-репозитория> until_bot && cd until_bot
cp .env.example .env   # впишите BOT_TOKEN
docker compose up -d --build
docker compose logs -f   # посмотреть логи
```

База лежит в папке `./data` и переживает перезапуски и обновления.
Обновить бота: `git pull && docker compose up -d --build`.

### Вариант 2 — systemd без Docker

```bash
sudo useradd -r -m -d /opt/until_bot untilbot
sudo -u untilbot git clone <url-репозитория> /opt/until_bot
cd /opt/until_bot
sudo -u untilbot python3 -m venv .venv
sudo -u untilbot .venv/bin/pip install -r requirements.txt
sudo -u untilbot cp .env.example .env   # впишите BOT_TOKEN
sudo cp deploy/until_bot.service /etc/systemd/system/
sudo systemctl enable --now until_bot
journalctl -u until_bot -f
```

### Важно

- Запускайте **только одну копию** бота с одним токеном — иначе Telegram вернёт ошибку `Conflict`.
- Чтобы друзья нашли бота, дайте им ссылку `https://t.me/<имя_бота>`.
- Описание и аватарку можно задать у @BotFather (`/setdescription`, `/setuserpic`).

## Тесты

```bash
pip install pytest
python -m pytest
```
