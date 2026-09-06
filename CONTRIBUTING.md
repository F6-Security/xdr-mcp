# Участие в разработке

## Окружение

Нужен Python 3.10+.

```sh
python3 -m venv venv
./venv/bin/pip install --require-hashes -r requirements.lock
./venv/bin/pip install --no-deps -e .
```

Зависимости ставятся из лока с проверкой хешей: сервер держит живой API-токен,
поэтому подменённая зависимость здесь — это компрометация доступа, а не просто
сломанная сборка.

## Тесты и линт

```sh
./venv/bin/python -m unittest discover -s tests -v
./venv/bin/ruff check .
```

Тесты не требуют сети и работают на stdlib `unittest`. Форматтер `ruff format`
намеренно не включён в CI.

## Зависимости

Версии задаются в `pyproject.toml`, `requirements.lock` из них генерируется.
После правки зависимостей лок нужно пересобрать, иначе CI это заметит:

```sh
uv pip compile pyproject.toml --universal --python-version 3.10 --generate-hashes -o requirements.lock
```

Флаги обязательны оба. `--universal` делает лок пригодным для всех платформ,
`--python-version` фиксирует нижнюю границу: без него uv резолвит под тот
интерпретатор, которым запущен, и результат зависит от машины.

## Контейнер

```sh
docker build -t xdr-mcp .
```

Образ публикуется под `linux/amd64` и `linux/arm64`.

## Проверить сервер вручную

Сервер общается по stdio, поэтому его можно подёргать напрямую:

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | XDR_BASE_URL=https://<your-xdr-host> XDR_API_KEY=<token> xdr-mcp
```

## Добавление секции или тула

Секции описаны словарём `SECTIONS` в `src/xdr_mcp/server.py`: базовый путь, путь
для `count`, поля сортировки, способ пагинации. Описания параметров генерируются
из этого словаря, чтобы документация не расходилась с кодом.

Имена полей и допустимые значения проверяйте по `xdr_get_mapping` и
`xdr_get_filters` на живой инсталляции. API не отвергает несуществующее поле или
значение — он отвечает пустым результатом, неотличимым от «ничего не найдено»,
поэтому ошибка в описании тула превращается в тихо неверный ответ модели.

## Документация

`README.md` (русский) и `README.en.md` — одно и то же содержимое на двух языках,
разделы совпадают один в один. Правку нужно вносить в оба файла. На PyPI уходит
русская версия: она указана в `readme` в `pyproject.toml`.

Оба файла публикуются и на GitHub, и на PyPI, поэтому:

- все ссылки абсолютные — PyPI не резолвит относительные пути;
- картинки только по абсолютному URL и только PNG или JPEG — SVG PyPI не покажет;
- без разметки, специфичной для GitHub (`> [!NOTE]`, `<picture>`) — санитайзер
  PyPI её вырежет.

Проверить, как страницу отрисует PyPI:

```sh
./venv/bin/python -m build && ./venv/bin/twine check dist/*
```

Логотип: `docs/logo.svg` — исходный знак с сайта, `docs/logo-banner.svg` — версия
для шапки, белый знак на фирменном тёмном, чтобы читаться и в светлой, и в тёмной
теме. `docs/logo.png` генерируется из баннера:

```sh
rsvg-convert -w 880 -h 264 docs/logo-banner.svg -o docs/logo.png
```

## Релиз

Версия задаётся в `src/xdr_mcp/__init__.py` и оттуда попадает в метаданные
пакета, версию MCP-сервера и User-Agent. Публикация на PyPI и в GHCR происходит
по факту публикации GitHub Release.
