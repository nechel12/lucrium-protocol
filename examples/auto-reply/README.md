# Автоответчик

Отвечает на входящее сообщение покупателя шаблоном из настроек. Пауза на чат
защищает от серии ответов на несколько сообщений подряд.

## Настройки

| Ключ | Тип | По умолчанию | Назначение |
|---|---|---|---|
| `enabled` | boolean | `true` | включён ли плагин |
| `reply_text` | string | «Здравствуйте! Я на связи…» | текст ответа |
| `cooldown_seconds` | integer | `300` | пауза на чат, сек |
| `skip_if_contains` | string | `""` | не отвечать, если в сообщении есть подстрока |

Форма настроек рендерится панелью из [`config_schema.json`](config_schema.json).

## Что демонстрирует

- `ctx.session.call` — вызов клиента aiosellers сессии (метод зависит от площадки:
  `chats.send_message` у FunPay/Playerok, `chats.send` у GGsel);
- `ctx.storage` — пауза на чат;
- `ctx.config` / `ctx.log`.

## Тест

```bash
uv run pytest examples/tests -q
```
