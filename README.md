# GASUCHKA — расписание СПбГАСУ 3-СУЗСс-2

Автоматическая система мониторинга расписания, посещаемости и аттестаций для группы 3-СУЗСс-2.  
Данные берутся из личного кабинета [portal.spbgasu.ru](https://portal.spbgasu.ru).

## Что умеет

- Парсит расписание (прошлая неделя + текущая + 2 следующие) → синхронизирует в Яндекс.Календарь
- Отслеживает изменения в расписании (переносы, замены, отмены) → уведомление в Telegram-канал
- Парсит журналы посещаемости и аттестаций → личное уведомление при новом пропуске или аттестации
- Запускается автоматически 3 раза в день через GitVerse CI/CD

## Расписание обновлений

| Время МСК | Cron |
|-----------|------|
| 07:00 | `0 4 * * *` |
| 13:30 | `30 10 * * *` |
| 21:00 | `0 18 * * *` |

## Подписка на расписание

**Яндекс.Календарь** — синхронизируется напрямую через CalDAV, обновляется мгновенно после каждого запуска CI.

**ICS-файл** (для других приложений):
```
https://gitverse.ru/api/repos/volobanov5/spbgasu-schedule/raw/branch/main/schedule.ics
```

## Telegram-канал

[@gasu4ka](https://t.me/gasu4ka) — уведомления об изменениях в расписании для всей группы.

## Файлы проекта

| Файл | Назначение |
|------|------------|
| `parse_portal.py` | Парсер расписания (Playwright → portal.spbgasu.ru) |
| `parse_journals.py` | Парсер журналов посещаемости и аттестаций |
| `sync_yandex.py` | Синхронизация ICS → Яндекс.Календарь через CalDAV |
| `notify.py` | Уведомления: расписание → канал, журналы → личка |
| `schedule.ics` | Текущее расписание |
| `journals_state.json` | Текущее состояние журналов (посещаемость, аттестации) |
| `.gitverse/workflows/update_schedule.yml` | CI/CD pipeline |

## Как работает CI

```
Checkout
  └─ Сохранить старые schedule.ics и journals_state.json
       └─ parse_portal.py → новый schedule.ics
            └─ parse_journals.py → новый journals_state.json
                 └─ notify.py → уведомления если что-то изменилось
                      └─ sync_yandex.py → обновить Яндекс.Календарь
                           └─ git commit + push если были изменения
```

## Секреты GitVerse

| Секрет | Описание |
|--------|----------|
| `PORTAL_LOGIN` | Логин от portal.spbgasu.ru (студенческий номер) |
| `PORTAL_PASS` | Пароль от портала |
| `YANDEX_LOGIN` | Логин Яндекса (без @yandex.ru) |
| `YANDEX_APPPASS` | Пароль приложения Яндекса для CalDAV |
| `TG_TOKEN` | Токен Telegram-бота @gasu4ka_bot |
| `TG_CHANNEL` | Username канала (`@gasu4ka`) |
| `TG_OWNER_ID` | Telegram chat_id владельца для личных уведомлений |
| `GV_TOKEN` | Токен GitVerse для git push из CI |

## Смена группы

В `parse_portal.py` изменить константу (каждый сентябрь):
```python
# parse_portal.py не использует GROUP — фильтрация идёт по логину студента
# просто обновить PORTAL_LOGIN / PORTAL_PASS в секретах GitVerse
```
