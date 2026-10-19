# Server-setings — эталон Ubuntu / 1Panel / OpenResty / Tailscale

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

## Архитектура после миграции и работа с доменом

- **Публичный 80/443:** один OpenResty, сайты и сертификаты строго под управлением 1Panel. Новый домен получает отдельный SNI-сайт и ACME-сертификат; IP-сайт сохраняется.
- **Офлайн-LAN:** только если необходим HTTPS по школьному IP — отдельный локальный Caddy, слушающий исключительно LAN IP:4610, не 80/443. Его жизненный цикл контролируется systemd и задокументирован; он не заменяет OpenResty.
- **Удалённое управление:** Tailscale/SSH/1Panel только для авторизованных администраторов, без публикации 19192 в WAN; установка/проверка отдельным скриптом.
- **Восстановление с нуля:** 1) чистая Ubuntu + Docker + 1Panel + Tailscale, 2) восстановление проверенных данных ASA Lab, 3) настройка публичных сайтов и LAN TLS. Пропуск восстановления БД запрещён.

[Схема IP, домена и LAN](docs/11-ЕДИНЫЙ-INGRESS-ДОМЕН-И-LAN.md) · [Удалённое управление](docs/12-TAILSCALE-И-УДАЛЕННЫЙ-ДОСТУП.md).

**Проверяемый план второго этапа без изменений системы:**

```bash
bash scripts/edge-setup.sh --profile home-static --public-ip 94.29.44.211 --lan-ip 192.168.1.115 --lan-cidr 192.168.1.0/24 --lan-https
python3 scripts/public-ipctl.py plan --public-ip 94.29.44.211
python3 scripts/domainctl.py plan --domain lab.alikinas.ru --public-ip 94.29.44.211
```

Домен в примере не считается подтверждённым доменом тестового сервера: реальные DNS/сертификат применяются **только после выбора имени владельцем, подготовки origin ASA Lab и проверки NAT**.

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
