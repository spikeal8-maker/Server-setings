# Контрольная точка программы безопасности домашнего сервера

2 ноября 2026. Статус: INCOMPLETE, production readiness не принята.

## Подтверждено и применено

- Согласованная перезагрузка завершена, ядро 7.0.0-42-generic. SSH по ключу/sudo, root MCP(uid0), 1Panel и ASA повторно проверены после почти часа uptime. PostgreSQL и остальные ASA containers healthy; /health/ready HTTP200/ready/database up.
- HTTPS 1panel.asa-lab.ru: валидный SAN/CA, TLS1.3, HSTS, HTTP→HTTPS, вход HTTP200, неавторизованный settings API401. Владелец ранее подтвердил успешный вход. Прямой порт панели снаружи не открыт.
- Настройка отключения MPTCP сохраняется, текущее значение0; повторный reboot после этой отдельной меры не выполнялся.
- Полный зашифрованный backup прошёл cloud roundtrip, USB-decrypt и изолированное восстановление PostgreSQL/MinIO; временные ресурсы и plaintext tar удалены. Timer enabled, следующий запуск 03.11.2026 02:30 МСК. Первый автоматический запуск пока не наблюдался. См.docs44.
- Инфраструктурные source-процедуры и фактические результаты опубликованы main через PR49,50,51,53,54. Реальные Ubuntu unit tests исправленного rehearsal-кандидата:406 PASS. Это не тесты всего приложения и не clean-machine reinstall.

## Дополнительная проверка сетевой экспозиции

Read-only Docker inspect: API/PostgreSQL/MinIO/search не имеют published ports; web4610 и Scratch4613 привязаны к127.0.0.1. FRP пользовательские порты14610/14620/14630 loopback; управляющий7000 привязан только к Tailscale100.75.149.26. Gateway/OpenResty host-network просматриваются отдельно через ss/UFW.

TCP connect с D2-R2 к публичному адресу домашнего роутера:80/443 reachable;22/19192/4610/5432/9000/9001/39443/7000/7500 timeout. Это проверка с владельческого компьютера, возможно через NAT hairpin; она не заменяет независимый Internet/IPv6 тест и полный внешний пен-тест. UFW active/default deny incoming. Privileged containers и Docker socket mounts среди работающих контейнеров не обнаружены.

## Открытые причины, почему боевую готовность ещё нельзя объявить

1. На работающих immutable images остаются HIGH/CRITICAL и EOL, включая MinIO/Scratch/gateways/OpenResty; отдельно найден уязвимый host-mounted MCP binary. Полный инвентарь и пределы сканирования:docs45. Source candidate не означает deployed fix.
2. ASA source PR958 (dependencies), PR957 (CSP), PR959 (runtime images/bridge) проходят отдельные gates. CSP общий CI имеет независимый QR/layout blocker; его нельзя обходить или чинить в security diff. Финальный CI образов и приложения не объявляется PASS до actual results точного SHA. Старый успешный image scan относится только к своему candidate SHA.
3. Applicable product deployment/acceptance blockers в ASA current.yaml (включая Electronics qualification) не сняты этим инфраструктурным отчётом. Рабочие образы не переключались на security-кандидаты. Повторное развёртывание требует собственного разрешённого deployment path/gates.
4. MFA 1Panel остаётся ручным действием владельца; ранее владелец сообщил, что сейчас не может привязать аутентификатор. Обход или фиктивное включение не выполнялись.
5. Часть контейнеров ещё использует неограниченные json-file logs. Нужны ограничение/проверка применения, итоговая пользовательская приёмка, alerting backup failures и проверка восстановления на второй чистой машине.

MinIO требует отдельного compatibility-first исследования: upstream server/client repositories архивированы, смена Alpine/Go без исправления старого module graph не закрывает находки. Автоматическая замена S3-backend не выполняется. Оригинальные volumes не используются для испытания кандидатов.

SSH и полный административный MCP сохраняются. Эти полномочия означают, что доступ к соответствующим ключам/токенам равнозначен управлению сервером; они не публикуются в GitHub.

