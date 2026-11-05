# Усиление безопасности: состояние2ноября2026

Общая задача **не завершена**; боеготовность не подтверждена. Поручение владельца включает исправление замечаний отчёта28 и сохранение SSH/полногоMCP. Двухфакторную привязку владелец пока выполнить не может; она не включалась автоматически.

## Проверенное исправлениеSSH

Независимо проверенный script scripts/harden-ssh.py, sourcec5cd72e, установлен в root-controlled каталоге /opt/server-settings/ssh-hardening-c5cd72e. Без параметров даёт план. Изменяет только свой drop-in /etc/ssh/sshd_config.d/00-asa-security.conf; сохраняет исходное содержимое в root-only /var/backups/asa-ssh-hardening/before.json.

Применение требует root, точногоhostname и предварительной проверки НОВОГО сеанса по ключу владельца. Перед reload ставится180-секундныйsystemdrollback; sshd-t и effective config проверяются до reload. Общийflock сериализует apply/confirm/rollback; весь путь timer-script должен принадлежать root и быть недоступен на запись другим. После нового SSH+sudo и независимогоrootMCP выполняетсяconfirm. При отказе не подтверждать: timer возвращает исходныйdrop-in и reloadSSH. Возврат вручную: --action rollback до подтверждения; послеconfirm восстановить записанный прежнийdrop-in вручную черезrootMCP/консоль и проверитьsshd-t передreload.

```bash
sudo python3 scripts/harden-ssh.py
# Сначала реальный новый key-only SSH и проверка отдельного full-rootMCP
sudo python3 /opt/server-settings/ssh-hardening-REVIEWED_SHA/harden-ssh.py \
  --action apply --expected-host ACTUAL_HOSTNAME --verified-owner-key
# Проверить новые соединенияSSH/MCP; подтвердить за180секунд
sudo python3 /opt/server-settings/ssh-hardening-REVIEWED_SHA/harden-ssh.py \
  --action confirm --expected-host ACTUAL_HOSTNAME
```

Теперь PasswordAuthentication=no, KbdInteractiveAuthentication=no, PubkeyAuthentication=yes, PermitRootLogin=no, X11Forwarding=no, MaxAuthTries3, LoginGraceTime30. SSH пользователяadmins с полнымsudo и port forwarding сохранён. Пароль самого пользователя/sudo не менялся; запрет касается способа удалённогоSSHвхода. Не запускать старый password-only remote helper; использовать ключ.

Фактические доказательства: свежий SSHпо ключу иsudoUID0 успешны послеreload; попытка password-only возвращает только разрешённыйpublickey. Реальный MCP root-operation4dd640f755be44e9ab08ca45b638c397 success/UID0; ssh,1panel-core,asa-operations active. После этих доказательств timer отключён confirm. Docker/UFW/DNS/Tailscale/порты/приложения/БД не менялись.

## Что остаётся выполнить

- ОС: свежие индексыAPT, securitypatches и контролируемая приёмка после возможногоreboot.
- Код/образы: обновить зависимости, CSPглавногоHTML, trustedproxy, сканирование фактических образов и отрицательные проверки прав.
- Данные: согласованная программой ежедневная encrypted cloud backup, полный rehearsal восстановления отдельной копииPostgreSQL+MinIO, мониторинг ошибок и retention без удаления прежних пользовательскихархивов.
- Администрирование: MFA1Panel/аккаунтов и проверка старых ключей/сессий; у владельца пока нет возможности привязатьаутентификатор. Не отключать полныйMCP.
- Независимая внешняя сеть/IPv6, алерты и восстановление после отказа питания ещё не приняты.

Исполнители продуктовых dependency/CSP срезов остановлены лимитом сервиса; их работа не выдана за выполненную. Исходные отдельные worktree сохранены для продолжения. Результат текущего среза — SSHhardening, а не закрытие всего списка безопасности.
