# Nexus Auto Uploader

Сервис для Linux: опрашивает один или несколько каталогов и загружает Docker-образы и Python-пакеты в Nexus. Работает без Docker daemon/socket внутри контейнера: образы переносит `skopeo`, Python-пакеты отправляет Twine по стандартному multipart-протоколу PyPI.

## Возможности

- Docker `docker save`: `.tar`, `.tar.gz`; все образы и теги из архива.
- PyPI: `.whl`, source distributions `.tar.gz` и `.zip`.
- Один смешанный входной каталог или несколько каталогов через `INPUT_DIRS`. Рекурсивного обхода нет.
- Добавление/замена namespace-префикса; исходный registry заменяется целевым, теги сохраняются.
- Проверка стабильности размера/mtime/inode перед обработкой; polling подходит для bind mounts и NFS, где inotify ненадёжен.
- Перенос каждого обработанного файла в `success/job-<uuid>/` или `error/job-<uuid>/` вместе с `result.json`. Одинаковые имена файлов не перезаписывают друг друга.
- Ограниченные повторные попытки, таймауты, восстановление очереди после перезапуска, защита от одновременного запуска на общих входных/processing-каталогах.
- TLS включён по умолчанию, общий и отдельные переключатели для Docker/PyPI, корпоративный CA.

## Требования к Nexus

1. Docker **hosted** repository и его push endpoint — `host[:port]`, например `nexus.example.org:5000`. Это не URL `/repository/...`. Обычно необходимо включить Docker Bearer Token Realm и выдать учётной записи права чтения/добавления/изменения нужного repository.
2. PyPI **hosted** upload endpoint, например `https://nexus.example.org/repository/pypi-hosted/`. **Не `/simple/`**, не proxy и не group для обычной загрузки.
3. Учётная запись с минимально необходимыми правами. Anonymous write можно использовать только если это намеренно настроено в Nexus.
4. Политика redeploy в Nexus определяет, разрешены ли повторные версии/теги. Сервис не удаляет существующие пакеты и не обходит эту политику.

## Быстрый деплой с Docker Compose

Нужны Docker Engine и Compose plugin (`docker compose`). Из каталога проекта:

```bash
cp .env.example .env
chmod 600 .env
mkdir -p data/{input,processing,success,error} certs
sudo chown -R 10001:10001 data
# Заполните .env локально: endpoints и credentials.
docker compose --env-file .env config --quiet
docker compose build
docker compose run --rm uploader --check-config
docker compose up -d
docker compose logs -f --tail=100 uploader
```

Не публикуйте вывод `docker compose config` без `--quiet`: он содержит подставленные пароли. `.env`, рабочие данные и сертификаты исключены из Git и Docker build context. Контейнер работает под UID/GID 10001 с read-only root filesystem и без привилегий. `UPLOADER_UID`/`UPLOADER_GID` позволяют выбрать другой UID/GID; выставьте соответствующие права на **все** data-каталоги.

`compose.yaml` монтирует `./data/input`, `./data/processing`, `./data/success`, `./data/error` и `./certs`. Для существующих каталогов на сервере измените левые части bind mounts или используйте Compose override. Пути в `INPUT_DIRS` должны быть видны внутри контейнера.

На RHEL с SELinux применяйте подходящую политику/метки для bind mounts (`:Z` для эксклюзивных, `:z` для совместно используемых), не отключайте SELinux целиком. Для NFS согласуйте UID/GID/ACL и SELinux-политику с администратором; root squash может запрещать `chown`. Используйте одну реплику сервиса и файловую систему с рабочими advisory locks. Это не распределённая HA-очередь.

Остановка и обновление:

```bash
docker compose stop
docker compose up -d --build
```

SIGTERM прекращает приём новых заданий после текущей обработки. Compose даёт час на завершение; для крупных архивов настройте `stop_grace_period`. Принудительное завершение может привести к повторной загрузке после восстановления.

## Конфигурация

Все настройки читаются из environment при старте. Compose явно передаёт настройки из `.env`; локальная CLI сама `.env` не загружает.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `INPUT_DIR` | `./data/input` (Compose: `/data/input`) | Единственный входной каталог |
| `INPUT_DIRS` | пусто | CSV путей; если задан, **заменяет** `INPUT_DIR`, например `/data/input/docker,/data/input/pypi` |
| `PROCESSING_DIR` | `./data/processing` | Захваченные задания и сохранённые результаты |
| `SUCCESS_DIR` | `./data/success` | Успешные задания |
| `ERROR_DIR` | `./data/error` | Неудачные/частично успешные задания |
| `DOCKER_REGISTRY` | пусто | Целевой Docker push registry `host[:port]` |
| `DOCKER_USERNAME`, `DOCKER_PASSWORD` | пусто | Docker credentials |
| `PYPI_URL` | пусто | URL загрузки в hosted PyPI repository |
| `PYPI_USERNAME`, `PYPI_PASSWORD` | пусто | PyPI credentials |
| `TLS_VERIFY` | `true` | Общая настройка проверки TLS |
| `DOCKER_TLS_VERIFY`, `PYPI_TLS_VERIFY` | наследуют `TLS_VERIFY` | Отдельные настройки; пустое значение наследует общую |
| `CA_BUNDLE` | пусто | Путь к PEM CA bundle внутри контейнера/на локальной машине |
| `IMAGE_PREFIX_MODE` | `prepend` | `prepend` или `replace` |
| `IMAGE_PREFIX` | пусто | Новый namespace-префикс, без целевого registry |
| `IMAGE_SOURCE_PREFIX` | пусто | Старый префикс для `replace`; обязателен в этом режиме |
| `POLL_INTERVAL` | `2` | Интервал опроса в секундах, положительное число |
| `STABLE_SECONDS` | `10` | Сколько секунд сигнатура файла должна оставаться неизменной |
| `UPLOAD_TIMEOUT` | `300` | Таймаут одной попытки `skopeo`; HTTP read timeout (connect — не более 10 с), не общий дедлайн всей очереди |
| `UPLOAD_RETRIES` | `2` | Дополнительные попытки после первой, от 0 до 10; backoff 1/2/4/8 с |
| `SKOPEO_BIN` | `skopeo` (Compose: `/usr/bin/skopeo`) | Исполняемый файл skopeo |
| `TMPDIR` | системный temp; контейнер: `/data/processing` | Writable disk для authfile и image-sized временных файлов |
| `UPLOADER_UID`, `UPLOADER_GID` | `10001` | Пользователь контейнера (только Compose) |

Каталоги очереди и все входные каталоги не должны совпадать или быть вложены друг в друга. Если один протокол не настроен, относящийся к нему файл попадёт в error с соответствующей причиной; другой протокол продолжит работать.

### Примеры префиксов

При `DOCKER_REGISTRY=nexus.example.org:5000`:

```dotenv
IMAGE_PREFIX_MODE=prepend
IMAGE_PREFIX=mirror
```

- `team/app:v1` → `nexus.example.org:5000/mirror/team/app:v1`
- `old.example:5001/team/app:v1` → `nexus.example.org:5000/mirror/team/app:v1`
- `mirror/team/app:v1` → тот же namespace, без повторного `mirror/`.

```dotenv
IMAGE_PREFIX_MODE=replace
IMAGE_SOURCE_PREFIX=old-team
IMAGE_PREFIX=new-team
```

- `old-team/app:v1` → `nexus.example.org:5000/new-team/app:v1`
- `older-team/app:v1` → `nexus.example.org:5000/older-team/app:v1`: совпадение проверяется по границе `/`.
- Для исходного `old.example:5001/old-team/app:v1` можно указать как `old-team`, так и полный `old.example:5001/old-team`.
- Если старый префикс не совпал, namespace сохраняется (исходный registry всё равно заменяется целевым). Пустой новый префикс удаляет совпавший старый; итоговое имя должно оставаться корректным.

### TLS и корпоративный CA

```bash
cp /secure/path/company-ca.pem certs/company-ca.pem
# В .env: CA_BUNDLE=/certs/company-ca.pem, TLS_VERIFY=true
```

`CA_BUNDLE` применяется к обоим протоколам. Для временного отключения проверки задайте `TLS_VERIFY=false` либо отдельный флаг. Это небезопасно: отключаются проверка цепочки и hostname; у `skopeo` также возможен fallback на HTTP. Явный `http://` в `PYPI_URL` всегда передаёт данные без TLS, независимо от verify. Предпочитайте корректный HTTPS и доверенный CA.

## Как подавать файлы

**Надёжный контракт — сначала запись во временное имя, затем atomic rename в том же файловом разделе.** Не перезаписывайте ещё не обработанные входные файлы и не продолжайте запись через открытый FD после публикации; используйте уникальные имена. Ожидание стабильности — страховка, но пауза в медленном копировании дольше `STABLE_SECONDS` не означает завершения записи.

```bash
docker save -o data/input/images.tar.part team/app:v1 team/app:latest
mv data/input/images.tar.part data/input/images.tar

# Сжатый архив (pipefail защищает от ошибки docker save):
set -o pipefail
docker save team/app:v1 | gzip > data/input/images.tar.gz.part
mv data/input/images.tar.gz.part data/input/images.tar.gz

cp dist/example-1.0-py3-none-any.whl data/input/example-1.0-py3-none-any.whl.part
mv data/input/example-1.0-py3-none-any.whl.part data/input/example-1.0-py3-none-any.whl
```

Для отдельных входных каталогов задайте `INPUT_DIRS=/data/input/docker,/data/input/pypi`, создайте эти подкаталоги и выставьте права UID/GID сервиса. Существующие файлы подхватываются при старте. Symlinks, подкаталоги и неподдерживаемые суффиксы игнорируются.

`.tar.gz` распознаётся по Docker `manifest.json`; иначе считается Python sdist. Это **не** `docker export` и не OCI archive. Нетегированные образы, некорректные/неоднозначные manifests и коллизии итоговых тегов отклоняются до отправки. Архивы не извлекаются в файловое дерево; gzip Docker save предварительно распаковывается в закрытый временный tar для совместимости с Bookworm skopeo.

## Результаты, ошибки и восстановление

```text
data/success/job-<uuid>/original.whl
data/success/job-<uuid>/result.json
data/error/job-<uuid>/images.tar.gz
data/error/job-<uuid>/result.json
```

В отчёте: исходное имя, тип, `status`, длительность, причина ошибки и результаты по каждому Docker-тегу (source tag, destination, status). Логи в stdout содержат timestamps и результаты; credentials и сырой ответ сервера/`skopeo` не публикуются.

Загрузка многотегового архива не является транзакцией: если хотя бы один тег не загрузился, **весь исходный архив** идёт в error, а `result.json` показывает, что уже загружено. Rollback удалённых тегов не выполняется. После устранения причины скопируйте исходный файл обратно через `.part` + rename; не переносите в input сам `result.json`/каталог задания.

Результат сохраняется перед переносом в terminal-каталог. Если этот перенос временно не удался, задание остаётся в processing и повторяется только перенос, без повторной отправки. Если результат нельзя записать, процесс завершается, чтобы не попасть в hot loop. При аварии между удалённой загрузкой и фиксацией результата возможен повтор: семантика **at least once**, а не exactly once. Аналогично сетевой обрыв после приёма пакета сервером может закончиться повторной попыткой и конфликтом версии.

Не удаляйте `.nexus-uploader.lock` работающего процесса. Не редактируйте вручную processing во время работы. Одновременная загрузка одного processing/input набора блокируется.

При переносе между файловыми системами копия сначала пишется в неподдерживаемое временное имя и только затем атомарно публикуется как готовый payload: обрыв копирования не отправит частичный архив. Подменённый при захвате файл не загружается; захваченная подмена отправляется в карантин error. Symlink не разыменовывается при восстановлении/архивировании. Если terminal-каталог с таким UUID уже существует, исходный processing удаляется только после проверки идентичности имён и содержимого; при конфликте он сохраняется с логом `terminal_collision`. Для локальной устойчивости выполняется fsync данных и каталогов; гарантии конкретной NFS/storage-системы всё равно зависят от её конфигурации.

**Диск:** нужны место для оригинального архива, распакованного tar, временной копии `skopeo` и terminal staging. Для gzip-образов резервируйте как минимум несколько размеров **распакованного** архива. Не направляйте image-sized temp в маленький RAM tmpfs. После аварийного убийства могут остаться временные файлы вне `job-*`; инспектируйте их и удаляйте только при остановленном сервисе. Success/error автоматически не очищаются: настройте внешнюю retention-политику.

## Локальный запуск и тесты

Нужны Linux, Python 3.11+ (проверено 3.11 в контейнере и 3.13 локально), `uv` и `skopeo` (например, пакет дистрибутива). Установка зависимостей воспроизводима по `uv.lock`:

```bash
uv sync --frozen
# Экспортируйте переменные вручную или безопасно загрузите свой shell-compatible .env:
set -a
. ./.env
set +a
uv run --frozen nexus-auto-uploader --check-config
uv run --frozen nexus-auto-uploader
# Две проверки с паузой стабильности, затем выход:
uv run --frozen nexus-auto-uploader --once

uv run --frozen pytest -q --cov=nexus_uploader --cov-report=term-missing
uv run --frozen ruff check src tests
uv build
```

При локальном запуске используйте локальные значения путей: `/certs/...` и `/data/...` в Compose относятся к контейнеру. Если `UV_INDEX`/`UV_INDEX_URL` переопределяют ваш package index, проверьте эти настройки при ошибках установки, не публикуя credentials.

`--check-config` проверяет синтаксис настроек, но не связь с Nexus/права на запись. `--once` возвращает 0 при завершённом проходе даже при ошибках отдельных файлов: проверяйте `result.json`; 1 — фатальная ошибка очереди/lock, 2 — неверная конфигурация.

Тесты используют временные файлы, реальный локальный HTTP multipart upload и управляемый subprocess seam; реальные Nexus credentials не нужны. Для проверки конкретного Nexus после деплоя отправьте небольшой тестовый Docker save/пакет, проверьте `result.json`, наличие тегов/версии в Nexus и сделайте pull/download точного артефакта.

## Лицензия и участие

Собственный код проекта распространяется по [MIT](LICENSE), Copyright (c) 2026 Lazza351.
Лицензия разрешает коммерческое использование и модификации с сохранением уведомления;
гарантии не предоставляются. Сторонние зависимости и компоненты контейнера сохраняют
свои лицензии — MIT проекта их не заменяет.

- [Правила участия и проверки](CONTRIBUTING.md)
- [Безопасность и сообщение об уязвимостях](SECURITY.md)
