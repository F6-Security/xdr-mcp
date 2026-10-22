<p align="center">
  <img src="https://raw.githubusercontent.com/F6-Security/xdr-mcp/HEAD/docs/logo.png" alt="F6" width="220">
</p>

# F6 XDR MCP Server

<p align="right"><a href="https://github.com/F6-Security/xdr-mcp/blob/HEAD/README.en.md">English</a></p>

[![PyPI](https://img.shields.io/pypi/v/xdr-mcp)](https://pypi.org/project/xdr-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/xdr-mcp)](https://pypi.org/project/xdr-mcp/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/F6-Security/xdr-mcp/blob/HEAD/LICENSE)
[![CI](https://github.com/F6-Security/xdr-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/F6-Security/xdr-mcp/actions/workflows/ci.yml)

MCP-сервер к платформе [F6 XDR](https://f6.security). Даёт ИИ-ассистенту доступ к
алертам, инцидентам, письмам, файлам, хостам и журналу — поиском на языке запросов
платформы.

## Тулы

| Тул | Назначение |
| --- | --- |
| `xdr_search` | Поиск по секции; пустой запрос возвращает секцию целиком |
| `xdr_count` | Количество записей |
| `xdr_get_mapping` | Доступные поля поиска |
| `xdr_get_filters` | Доступные значения фильтров |
| `xdr_get_suggestions` | Реальные значения поля по префиксу |
| `xdr_list_artifacts` | Артефакты, собранные с хостов: багрепорты, логи, конфиги, дампы |
| `xdr_get_artifact_structure` | Файлы и папки внутри собранного артефакта |
| `xdr_download_artifact` | Скачать артефакт или его часть в локальную папку |
| `xdr_mark_event` | Пометить объект: закрыть алерт, отметить ложное срабатывание |

Секции: `alerts`, `incidents`, `emails`, `files`, `events`, `connections`, `assets`,
`modules`, `audit`, `applications`.

Первые семь тулов только читают. `xdr_download_artifact` пишет файлы на диск,
`xdr_mark_event` изменяет данные; оба по умолчанию выключены, см. раздел
«Безопасность».

## Быстрый старт

Нужен Python 3.10+ и персональный API-токен пользователя XDR.

```json
{
  "mcpServers": {
    "xdr": {
      "command": "uvx",
      "args": ["xdr-mcp"],
      "env": {
        "XDR_BASE_URL": "https://<your-xdr-host>",
        "XDR_API_KEY": "<token>"
      }
    }
  }
}
```

Этот блок подходит для Claude Desktop, Cursor и VS Code. Для Claude Code:

```sh
claude mcp add xdr \
  -e XDR_BASE_URL=https://<your-xdr-host> -e XDR_API_KEY=<token> \
  -- uvx xdr-mcp
```

Вместо `uvx` можно использовать `pip install xdr-mcp` и команду `xdr-mcp`, либо
контейнер — `docker run -i --rm -e XDR_BASE_URL -e XDR_API_KEY ghcr.io/f6-security/xdr-mcp`
(образ собран под `linux/amd64` и `linux/arm64`).

## Переменные окружения

| Переменная | Назначение |
| --- | --- |
| `XDR_BASE_URL` | Адрес вашей инсталляции XDR |
| `XDR_API_KEY` | Персональный API-токен пользователя |
| `XDR_ALLOW_WRITE` | `1` включает `xdr_mark_event`. По умолчанию выключен |
| `XDR_DOWNLOAD_DIR` | Папка, куда `xdr_download_artifact` сохраняет артефакты; заданная, включает тул. В контейнере — путь внутри примонтированного тома |
| `XDR_CA_BUNDLE` | PEM-бандл для инсталляции за внутренним УЦ |

## Примеры запросов

```
alerts        severity : "critical" AND resolved : "false"
alerts        timestamp >= now-1d AND NOT false_positive : "true"
incidents     closed : "false" AND timestamp >= now-30d
emails        is_blocked : "true" AND timestamp >= now-1d
assets        edr_active : "true"
applications  vendor : "Microsoft Corporation"
audit         success : "false" AND timestamp >= now-7d
```

Условия объединяются через `AND`, `OR`, `NOT` и группируются скобками. Время —
поле `timestamp`, относительное (`now-1d`, `now-6h`) или абсолютное. Какие поля
доступны в секции, покажет `xdr_get_mapping`.

В строковых значениях работает `*` в любой позиции: `name : "VDI-5*"`,
`file_name : "*.exe"`. Без него значение должно совпасть точно. Если запрос ничего не
вернул, а значение могло писаться иначе, точное написание подскажет
`xdr_get_suggestions`: платформа отвечает на несовпавшее значение нулём записей, а не
ошибкой.

## Безопасность

Всё, что возвращают читающие тулы, попадает в контекст языковой модели: адреса и
темы писем, имена файлов, хосты, журнал действий. Выдавайте серверу токен с
минимально необходимыми правами.

`xdr_mark_event` создаёт матчер — правило, которое применяется ко всем объектам,
подходящим под выражение, и этим тулом не отменяется. Поэтому тул выключен по
умолчанию; включайте `XDR_ALLOW_WRITE` осознанно и не добавляйте его в
авто-подтверждение MCP-клиента.

Собранные артефакты — одни из самых чувствительных данных платформы: дампы памяти,
учётные данные, история команд. `xdr_download_artifact` кладёт их на диск, а модели
возвращает только путь и размер — содержимое в контекст не попадает. Тул включается,
только когда задана `XDR_DOWNLOAD_DIR`; выбирайте для неё папку с ограниченным
доступом. Для списка и скачивания токену нужны права на артефакты.

Модель угроз и порядок сообщения об уязвимостях —
[SECURITY.md](https://github.com/F6-Security/xdr-mcp/blob/HEAD/SECURITY.md).

## Участие в разработке

См. [CONTRIBUTING.md](https://github.com/F6-Security/xdr-mcp/blob/HEAD/CONTRIBUTING.md).

## Лицензия

[Apache License 2.0](https://github.com/F6-Security/xdr-mcp/blob/HEAD/LICENSE)
