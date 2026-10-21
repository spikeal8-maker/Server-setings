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

## Фактический результат

На тестовой Ubuntu применён backend2c7905b. План MCP2c32268d992b4fa588d6c673a577dbab возвращает ready=true, blockers=[],8 checks passed, целевой6e80e3f3/schema234. Superseded относится только к faf549a0812a4f1a88c94ccf03334074 от прежней установки: история сохраняется, повторное выполнение возвращает reused=true, успешная приёмка старой операции не подделывается.

Операция130d171bc41949a9b0e0c98f374e8a4d завершена success/accepted=true: teacher login, student login, course open, project open, Scratch save/reopen и electronics save/reopen PASS. Teacher использует настоящую форму и штатную клавиатурную активацию проверки; student — обычный API login и браузерную навигацию. Используются только собственные разрешённые TEST-объекты. Для электроники плата Arduino подготовлена через штатный API этого проекта; код изменён через UI и проверен после серверного сохранения в новом контексте. Архивные проекты не редактировались.

Все60 infrastructure tests прошли на Ubuntu. Установщик ASA bbd4d6da5ec54670f149aff7bdc1c709b1e4fbd7 прошёл87 тестов в GitHub на Windows/Ubuntu и Docker job сборки, отдельного запуска и полного backup/restore: run39668368304. Это изолированный CI/dev smoke, не доказательство второй production-ОС. Production credential validator отдельно проверен новым unit-тестом. Общее CI и включение кандидатов в main учитываются отдельно.

Контейнеры ASA и приватный.env при установке backend не менялись; БД не заменялась. Только настройки обслуживания, безопасная ссылка Git, код backend и разрешённые тестовые данные.

## Совместимость 1Panel и DNS

list_databases теперь использует /api/v2/databases/db/search и orderBy=createdAt. Ответ1Panel с items=null/total0 нормализуется в пустой список. Результат — зарегистрированные в панели инстансы, а не поиск самостоятельного PostgreSQL Compose. Подпись отправляется только loopback; environment proxy и redirect отключены; password/address не возвращаются. Реальный вызов MCP вернул code200,total0; runtime_identity отдельно подтвердил PostgreSQL ASA.

Google и Cloudflare по DNS-over-HTTPS вернули A test.asa-lab.ru=94.29.44.211. Обычные DNS-запросы из Windows-среды дают refused; это не отсутствие публичной записи домена. DNS/VPN/hosts/firewall компьютера не менялись согласно правилам D2-R2. Ubuntu получает GitHub/registry и доверенный HTTPS entry; эта Windows-проблема не блокирует серверный update.

## Быстрый повтор на новом сервере

1. Подготовить поддерживаемую Ubuntu/Docker/1Panel и одну каноническую ASA установку по PORTABLE_OPERATIONS. Использовать проверенный published stable; не устанавливать случайный main поверх более новой схемы.
2. Задать один раз приватный entry_origin и согласованные Scratch origins, владельца и пути в private config. Никаких машинных секретов в GitHub. Поля образца templates/asa-operations.config.example.json адаптируются под узел.
3. Взять проверенный Server-setings commit, проверить plan, затем выполнить scripts/install-asa-operations.sh --apply. Он подготовит private backups, instanceId, tracking main и актуальный backend. Служебный state_root разрешён только в /var/lib/server-settings/asa-operations[-name]. Existing .env/архивные ключи не регенерируются.
4. Подготовить новый MCP update_plan и убедиться ready=true; затем deployment_verify. Операции первой установки, штатного обновления и замены данных не взаимозаменяемы. Для restore сохраняется отдельная проверенная процедура; автоматическое угадывание нового архива не добавлено.

Код опубликован в PR13 Server-setings и PR684 ASA Lab. До owner merge обычный main ещё не включает эти изменения; для воспроизведения кандидата нужен его конкретный SHA/ветка. Production leticia и школьный узел этой работой не изменяются. Полная установка на второй чистой ОС не заявляется проверенной.

## Финальная интеграция и прежний blocker сборки

Отдельный проверенный commit3274f73f удаляет недостижимую ветку внутреннего компонента SchematicEditor: единственный вызов использовал только notes. DOM заметок, подписи и callbacks сохранены; свежая сборка, lint/types/format и32 существующих профильных теста PASS. Размер файла217808/218000байт вместо218156; лимит не увеличен. Независимое ревью PASS.

Финальный кандидат e4d95c1c5c7a8160ef39c8bc8de576351c6da1be включает только непересекающиеся документационные изменения main1801b4c. Windows87deployment tests PASS (4platform skips), preflight SAFE_TO_START. Portable39671235480 PASS на финальном SHA: Windows/Ubuntu contracts, образы, отдельный запуск и полный backup/restore. General39671235342 прошёл governance и весь code gate, включая bundle. Data gate:6551 тест прошёл, но public-projects beforeAll90s и Teacher Home lifecycle60s завершились тайм-аутами; общий CI не считается зелёным. Браузерные jobs ещё выполнялись на момент фиксации. Learning rerun запущен после npm ECONNRESET на том же SHA. Новые повторные прогоны не запускались; merge не выполнен.
