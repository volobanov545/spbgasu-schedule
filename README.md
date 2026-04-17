# Расписание СПбГАСУ — 3-СУЗСс-2

Автоматический парсер расписания с [rasp.spbgasu.ru](https://rasp.spbgasu.ru/).  
Обновляется 3 раза в день через GitVerse CI/CD: в 07:00, 13:30 и 21:00 МСК.

Генерирует два календаря:
- `schedule.ics` — основное расписание (прошлая неделя + текущая + 2 следующие)
- `session.ics` — экзамены сессии (все экзамены начиная с сегодня)

## Подписка на календарь

### Основное расписание
```
https://gitverse.ru/api/repos/volobanov5/spbgasu-schedule/raw/branch/main/schedule.ics
```

### Сессия (экзамены)
```
https://gitverse.ru/api/repos/volobanov5/spbgasu-schedule/raw/branch/main/session.ics
```

### Яндекс.Календарь
1. Открыть [calendar.yandex.ru](https://calendar.yandex.ru)
2. Нажать **«+»** → **«Подписаться на календарь»**
3. Вставить URL выше → **«Подписаться»**

### Google Calendar
1. Настройки → **«Добавить календарь»** → **«По URL»**
2. Вставить URL выше

## Как работает

Парсер пробует источники данных по приоритету:

1. **Excel endpoint** — самый быстрый, без браузера (`/getExcel.php`)
2. **HTML через requests** — лёгкий запрос без JS-рендеринга
3. **Playwright** — полноценный браузер (Chromium headless), нужен если сайт требует JS
4. **Fallback HTML** — `saved_resource.html` из репозитория, если сайт недоступен

При наличии вкладки **«Сессия»** на сайте дополнительно парсятся экзамены → `session.ics`.

GitVerse CI/CD запускает парсер с российских серверов (сайт СПбГАСУ блокирует зарубежные IP).  
Если расписание изменилось — коммитит обновлённый `.ics` в репозиторий.

## Локальный запуск

```bash
# Установить зависимости
pip install -r requirements.txt
pip install openpyxl
playwright install chromium

# Автоматический режим (Excel → requests → Playwright → fallback)
python parse_schedule.py

# Только ближайшие 14 дней
python parse_schedule.py --days 14

# Принудительно через Playwright
python parse_schedule.py --playwright

# Из сохранённого HTML файла
python parse_schedule.py --file saved_resource.html

# Указать другой выходной файл
python parse_schedule.py --output my_schedule.ics --session-output my_session.ics
```

Или через готовый скрипт (делает git push после обновления):
```bash
bash run_update.sh
```

## Смена группы (каждый сентябрь)

В `parse_schedule.py` изменить константу `GROUP` — последняя цифра это номер курса:
```python
GROUP = "3-СУЗСс-2"  # → "3-СУЗСс-3" на 3 курсе, "3-СУЗСс-4" на 4-м
```
