# Расписание СПбГАСУ — 3-СУЗСс-2

Автоматический парсер расписания с [rasp.spbgasu.ru](https://rasp.spbgasu.ru/).  
Обновляется каждый день в 09:00 МСК через GitHub Actions.

## Подписка на календарь

**URL для подписки:**
```
https://<ВАШ_ЛОГИН>.github.io/<ИМЯ_РЕПО>/schedule.ics
```

### Яндекс.Календарь
1. Открыть [calendar.yandex.ru](https://calendar.yandex.ru)
2. Нажать **«+»** → **«Подписаться на календарь»**
3. Вставить URL выше → **«Подписаться»**

### Google Calendar
1. Настройки → **«Добавить календарь»** → **«По URL»**
2. Вставить URL выше

## Локальный запуск

```bash
# Установить зависимости
pip install -r requirements.txt
playwright install chromium

# Спарсить из сохранённого HTML
python parse_schedule.py --file /путь/к/saved_resource.html

# Спарсить живую страницу (нужен Playwright)
python parse_schedule.py --playwright

# Только ближайшие 14 дней
python parse_schedule.py --playwright --days 14
```

## Как работает

1. Playwright открывает `rasp.spbgasu.ru`, выбирает группу `3-СУЗСс-2`
2. BeautifulSoup парсит HTML: `.item` → `.days` → `.lesson`
3. Генерирует `schedule.ics` совместимый с Яндекс/Google Calendar
4. GitHub Actions коммитит файл если расписание изменилось
5. GitHub Pages отдаёт файл по публичному URL
