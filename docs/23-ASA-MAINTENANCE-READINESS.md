# Воспроизводимая подготовка ASA install/update

Дефекты2026-10-21: новая установка не имела backupDirectory; старый MCP recovery pin выбирал225 поверх текущей234; browser hook входил без bot proof, пытался открыть отсутствующий курс и редактировать Arduino без платы.

Изменения кандидата: scripts/prepare-asa-maintenance.py (plan default, --apply); scripts/asa_ops/maintenance.py (private backup folder, persistent instanceId, explicit verified replacement retirement); no-downgrade preflight; instance-bound start/verify/finalize/worker; browser hook uses actual teacher UI/keyboard checkbox and dedicated private course/Arduino project. Старые операции получают superseded, никогда success; исходные планы/ошибки/журналы сохраняются.

Обычная подготовка:

```bash
python3 scripts/prepare-asa-maintenance.py --config /etc/server-settings/asa-operations.json
# При --apply backend должен быть остановлен, detached workers отсутствовать.
python3 scripts/prepare-asa-maintenance.py --config /etc/server-settings/asa-operations.json --apply
```

Если действительно завершена отдельная чистая переустановка, оператор может добавить --replacement-receipt /private/stage/clean-install-receipt.json и --supersede-operation EXACT_ID. Требуются root-owned clean+technical квитанции,7 отсутствующих прежних контейнеров, новые2 volumes с совпадающим временем создания, текущий runtime/identity и операция старше новых volumes. Эта возможность не заменяет финализацию обычного failed update.

install-asa-operations.sh --apply теперь останавливает только backend, проверяет отсутствие его работ, готовит обслуживание и загружает актуальный код. Docker/ASA app/DB/OpenResty не перезапускаются. Изменяемые site/vhost/DNS файлы не редактируются.

Первая установка на новой Ubuntu с Docker выполняется штатным asa-lab/tools/asa_manager.py install --profile production (документация PORTABLE_OPERATIONS). Затем backend подключается через этот репозиторий. MCP update-план не считается первоначальной установкой: рабочий PostgreSQL определяется по Compose identity, а не каталогу БД приложений1Panel. Нельзя создавать рядом ещё один PostgreSQL/MySQL для уже работающего проекта.

На момент подготовки коммита кандидат ещё не применён к стенду; фактические результаты будут записаны отдельно. Полная установка на второй чистой ОС не заявляется проверенной.
