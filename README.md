# Server-setings — эталон Ubuntu / 1Panel / OpenResty / Tailscale

> **Доступ ИИ через MCP — см. [инструкцию для владельца через интерфейс](docs/13-CHATGPT-MCP-ДОСТУП-ЧЕРЕЗ-ИНТЕРФЕЙС.md), [журнал реальных проблем и проверок](docs/14-ЖУРНАЛ-СБОЕВ-И-ПРОВЕРОК.md) и [главную задачу прямого подключения #9](https://github.com/spikeal8-maker/Server-setings/issues/9).** Пока API 1Panel работает из D2-R2, но прямое подключение ChatGPT через MCP **НЕ завершено**: `/mcp` на публичном IP возвращал 404. `mcp-1panel` предоставляет фиксированный Bearer, а защищённое подключение custom MCP в ChatGPT нуждается в совместимой OAuth-авторизации. Сначала проверять право аккаунта/рабочей области создавать пользовательский MCP-плагин. Не выдавать существующий API-ключ за готовое подключение.


**Назначение:** русскоязычный инфраструктурный репозиторий для переноса проверенной конфигурации тестового сервера на новую Ubuntu 24.04 LTS, включая VPS, домашний сервер с белым IP и узел с динамическим IP/CGNAT.

Репозиторий содержит **наблюдаемый эталон**, конфигурационные шаблоны, безопасные установщики и диагностику. **Это не дамп базы и не подтверждённый готовый образ ОС:** реальная установка на второй чистой Ubuntu ещё требует интеграционной проверки. Без `--apply` скрипты ничего не изменяют.

## Быстрый старт

Прочитать: **[Начать здесь](docs/00-НАЧНИ-ЗДЕСЬ.md)** · **[Эталон сервера](docs/01-ЭТАЛОН-ТЕСТОВОГО-СЕРВЕРА.md)** · **[Риски](SECURITY.md)** · **[Инструкция ИИ](AGENTS.md)**.

```bash
# На любой машине — безопасный план
python3 scripts/serverctl.py plan --profile home-static
python3 scripts/serverctl.py plan --profile home-dynamic
python3 scripts/serverctl.py plan --profile vps

# Только на НОВОЙ, чистой Ubuntu 24.04 с root и проверенным аварийным доступом
sudo bash install.sh --profile home-static --apply

# Диагностика существующего Ubuntu-сервера без изменений
python3 scripts/serverctl.py doctor
python3 scripts/serverctl.py repair-public-caddy
```

Сначала устанавливаются Docker, Tailscale и закреплённая **1Panel v2.3.2** (официальный архив + SHA256, сверяемый также с `versions.lock.json`); затем владелец один раз авторизует Tailscale и создаёт отдельный API-ключ в 1Panel. Второй этап устанавливает управляемый панелью OpenResty, proxy-сайт, TLS, локальный MCP `readonly` и резервирование. Отдельные этапы нужны, чтобы не выдавать пустую базу за восстановленную или открывать панель в интернет.

### Дальнейшая настройка

```bash
sudo python3 scripts/store-panel-key.py
sudo python3 scripts/panelctl.py inspect
sudo python3 scripts/panelctl.py install-openresty --apply
sudo python3 scripts/panelctl.py create-site --site-host 94.29.44.211 --site-upstream http://127.0.0.1:4610 --site-alias asa-lab-public-ip
sudo bash scripts/setup-mcp.sh --apply
sudo bash scripts/backup-settings.sh
```

Команда `create-site` без `--apply` показывает план. На текущем тестовом сервере сайт уже существует, менять его скриптом нельзя. Сертификат публичного IP и доступ из внешней сети требуют явного подтверждения ACME, DNS/NAT и проверки. Образ OpenResty и 1Panel должны управляться **внутри панели**, не создавать параллельный Caddy на 80/443.

## Состав

| Папка | Что хранится |
|---|---|
| `profiles/` | Три сценария сети; настоящие значения тестовой Ubuntu показаны отдельно |
| `snapshots/2026-10-19/` | Подтверждённые SSH/API параметры работающего сервера, без секретов |
| `templates/` | Эталоны vhost, proxy, локального Caddy и OpenResty Compose для сравнения |
| `scripts/serverctl.py` | Базовая установка, план, диагностика, защищённый ремонт устаревшего автозапуска |
| `scripts/panelctl.py` | Чтение API 1Panel, установка OpenResty и осторожное создание сайта |
| `scripts/setup-mcp.sh` | Официальный `mcp-1panel`, `readonly` и localhost HTTP с Bearer |
| `scripts/backup-settings.sh` | Шифрованный age-снимок выбранных конфигураций и SQLite |
| `docs/` | Эксплуатация, сети, HTTPS, восстановление, известные поломки, правила для ИИ |
| `tests/` | Unit-тесты и защита от случайного попадания секретов |

**Главное правило:** приложению ASA Lab принадлежат `PostgreSQL/MinIO/проекты/ученики`. Их нельзя переустановить через этот репозиторий, потому что требуется отдельный проверенный резервный снимок и процедуры репозитория [asa-lab](https://github.com/spikeal8-maker/asa-lab).

## Проверка пакета

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh scripts/*.sh
python3 -m compileall -q scripts
```

[Полная матрица доказательств и ограничений](docs/08-ПРОВЕРКИ-И-ОГРАНИЧЕНИЯ.md). Секреты и персональные данные в GitHub **не хранить**, даже в private repository.

Проверка обновления короткого IP SSL: `python3 scripts/check-certificate.py --host 94.29.44.211`; при необходимости ставится локальный systemd timer `sudo bash scripts/install-cert-monitor.sh --host 94.29.44.211 --apply` (логи без внешних уведомлений).
