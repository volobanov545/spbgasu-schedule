# GASUCHKA — расписание СПбГАСУ 3-СУЗСс-2

Автоматическая система мониторинга расписания, посещаемости и аттестаций для группы 3-СУЗСс-2.  
Данные берутся из личного кабинета [portal.spbgasu.ru](https://portal.spbgasu.ru).

## Что умеет

- Парсит расписание (прошлая неделя + текущая + 2 следующие) → синхронизирует в Яндекс.Календарь
- Отслеживает изменения расписании (переносы, замены, отмены) → уведомление в Telegram-канал [@gasu4ka](https://t.me/gasu4ka)
- Парсит журналы посещаемости и аттестаций → личное уведомление при новом пропуске или аттестации
- Запускается автоматически 3 раза в день через GitVerse CI/CD
- **Telegram-бот [@gasu4ka_bot](https://t.me/gasu4ka_bot)** — личная статистика для каждого студента группы

## Расписание обновлений

| Время МСК | Cron |
|-----------|------|
| 07:00 | `0 4 * * *` |
| 13:30 | `30 10 * * *` |
| 21:00 | `0 18 * * *` |

## Telegram-бот — как подключиться

1. Напиши [@gasu4ka_bot](https://t.me/gasu4ka_bot) команду `/start`
2. Введи логин от портала (студенческий номер, например `24001234`)
3. Введи пароль от портала
4. Опционально: подключи Яндекс.Календарь (инструкция ниже)
5. Дождись подтверждения от администратора

После подтверждения доступны команды:
- `/stats` — посещаемость и аттестации в реальном времени
- `/connect_yandex` — подключить Яндекс.Календарь (если не сделал при регистрации)

### Как получить пароль приложения Яндекса

1. Открой [id.yandex.ru](https://id.yandex.ru)
2. Безопасность → Пароли приложений
3. Нажми «Создать пароль» → выбери «Другое»
4. Скопируй пароль из 16 символов — он вводится в боте

## Подписка на расписание

**Через бота** — подключи Яндекс.Календарь при регистрации, расписание появится автоматически.

**ICS-файл** (для других приложений):
```
https://gitverse.ru/api/repos/volobanov5/spbgasu-schedule/raw/branch/main/schedule.ics
```

## Архитектура

```
GitVerse CI (07:00 / 13:30 / 21:00 МСК)
  ├─ parse_portal.py   → schedule.ics
  ├─ parse_journals.py → journals_state.json
  ├─ notify.py         → pending_notification.json
  ├─ sync_yandex.py    → Яндекс.Календарь (владелец)
  └─ git push → GitVerse + GitHub (зеркало)

GitHub Actions (при изменении pending_notification.json)
  └─ send_notifications.yml → Telegram (серверы за РФ, Telegram доступен)

Amvera (бот, работает 24/7)
  └─ bot.py → регистрация, /stats, /connect_yandex
       ├─ parse_journals.py → Playwright → портал (личные данные)
       └─ sync_yandex.py   → CalDAV → Яндекс (личный календарь)
```

## Файлы проекта

| Файл | Назначение |
|------|------------|
| `parse_portal.py` | Парсер расписания (Playwright → portal.spbgasu.ru) |
| `parse_journals.py` | Парсер журналов; содержит `parse_lk_main(login, pass)` для бота |
| `sync_yandex.py` | Синхронизация ICS → Яндекс CalDAV; содержит `sync_calendar(login, pass)` |
| `notify.py` | Уведомления: расписание → канал, журналы → личка |
| `bot.py` | Telegram-бот с регистрацией и подтверждением |
| `db.py` | SQLite + Fernet-шифрование паролей пользователей |
| `Dockerfile` | Образ для Amvera (python:3.12-slim + Playwright Chromium) |
| `schedule.ics` | Текущее расписание |
| `journals_state.json` | Текущее состояние журналов |
| `.gitverse/workflows/update_schedule.yml` | GitVerse CI pipeline |
| `.github/workflows/send_notifications.yml` | GitHub Actions → Telegram |

## Секреты

### GitVerse CI
| Секрет | Описание |
|--------|----------|
| `PORTAL_LOGIN` | Логин от портала (студенческий номер) |
| `PORTAL_PASS` | Пароль от портала |
| `YANDEX_LOGIN` | Логин Яндекса (без @yandex.ru) |
| `YANDEX_APPPASS` | Пароль приложения Яндекса |
| `GV_TOKEN` | Токен GitVerse для git push из CI |
| `GH_TOKEN` | GitHub PAT (scope: repo + workflow) для зеркала |

### GitHub Actions
| Секрет | Описание |
|--------|----------|
| `TG_TOKEN` | Токен бота @gasu4ka_bot |
| `TG_CHANNEL` | `@gasu4ka` |
| `TG_OWNER_ID` | Telegram ID владельца |

### Amvera (переменные окружения)
| Переменная | Описание |
|------------|----------|
| `TG_TOKEN` | Токен бота |
| `TG_OWNER_ID` | Telegram ID владельца |
| `FERNET_KEY` | Ключ шифрования паролей пользователей |
| `DATA_DIR` | `/data` — путь к persistent storage |
