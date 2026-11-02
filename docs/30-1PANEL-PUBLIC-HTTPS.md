# Удалённый HTTPS-вход в1Panel

Проверено2ноября2026 на admins-MS-7D22. Имя1panel.asa-lab.ru указывает на94.29.44.211; опубликован отдельный сайт8 и сертификат8 в существующем1Panel/OpenResty. Панель проксируется к127.0.0.1:19192 через443. Прямой19192/firewall/SSH/Tailscale не менялись; приложенияASA и школьные сайты не перезапускались.1PanelAPI выполнял проверку конфигурации и штатную перезагрузку OpenResty; vhost-файлы и БД вручную не редактировались.

## Как входить

Базовый адрес https://1panel.asa-lab.ru. **Нужен прежний защищённый путь входа после имени домена:** полный адрес открыт владельцу в Chrome; сохраните его в личные закладки и используйте на другом компьютере. Голый корень сайта не является страницей входа: ответ-заглушка1Panel без entrance ожидаем. Путь не отключён и не раскрывается общедоступным redirect. В GitHub путь, пароли и ключи не публикуются. Логин/пароль панели прежние, MFA не отключалась и не настраивалась этой работой.

TLS завершается на OpenResty; upstreamHTTP проходит только внутри того же узла. HTTP301→HTTPS сохраняет путь. РазрешеныTLS1.2/1.3. В proxy-location явно заданы HSTS31536000 безincludeSubDomains, nosniff,SAMEORIGIN,no-referrer,no-store. Это важно: location-level add_header отменяет наследование серверных headers. Поддержаны WebSocket upgrade/длинные соединения; X-Forwarded-For/X-Real-IP перезаписываются входным адресом, а не произвольной строкой клиента. Для upstreamcookies заданы Secure/HttpOnly/SameSite=Lax; поведение конкретной авторизованной cookie не проверялось без входа.

## Исправление авторизации через HTTPS (2 ноября 2026)

У 1Panel v2.3.2 в браузерном коде функция `encryptPassword` читает публичный RSA-ключ из cookie `panel_public_key` через `document.cookie`. При отсутствии читаемой cookie она отправляет пароль без шифрования, тогда как сервер ожидает формат RSA/AES с тремя компонентами. В журнале входов это проявилось как `encrypted data format error` — не как проверка неверного пароля.

Первоначальное правило `proxy_cookie_flags ~ secure httponly samesite=lax;` выставляло `HttpOnly` всем cookie, в том числе `panel_public_key`. Исправлено точечно через штатный API 1Panel для управляемого сайта №8: **первое совпадение** для `panel_public_key` выставляет `secure nohttponly samesite=lax`, а все прочие cookie по-прежнему получают `secure httponly samesite=lax`.

Выявлен также второй дефект: при уже сохранённой браузером `HttpOnly`-cookie бэкенд 1Panel видит совпадающий публичный ключ и **не повторяет** `Set-Cookie`. Одной замены `HttpOnly` на `nohttponly` недостаточно, поскольку старая браузерная cookie продолжает отправляться и скрыта от JavaScript. В управляемом корневом прокси добавлено узкое правило: **только** для публичного GET `/api/v2/core/auth/setting` в upstream не передаётся заголовок Cookie, поэтому штатный middleware 1Panel повторно устанавливает `panel_public_key`. Остальные запросы сохраняют исходный Cookie. Дополнительный `location` не создаётся, чтобы 1Panel продолжала распознавать ровно один root-proxy. После применения проверена повторная выдача cookie с уже существующей cookie как через loopback, так и с D2-R2 по HTTPS. Это проверка исправления формата передачи, а не успешной авторизации владельца.

После изменения проверен HTTPS-запрос к `/api/v2/core/auth/setting`: статус 200, `panel_public_key` устанавливается с `Secure` и `SameSite=Lax`, без `HttpOnly`. Успешную интерактивную авторизацию владельца этой серверной проверкой не подтверждали — её выполняют отдельно без записи и передачи паролей. Не возвращайте глобальный `HttpOnly` на `panel_public_key` при повторной публикации сайта.

## Повторение на аналогичной установке

Требуется уже работающая1Panel на localhost19192, управляемый OpenResty80/443, DNSдомен→реальныйпубличныйIP, NAT/маршрутизация80/443 и **существующий** ACMEаккаунт в1Panel. Для новой машины использовать её фактический hostname/IP и IDеёACMEаккаунта. Этот script привязан к согласованному имени1panel.asa-lab.ru и loopback19192; другое имя/порт требует отдельной адаптации. НовыеACMEаккаунты/согласия не создаются.

Актуальный административный API-key сохраните отдельно root-only0600 (например /etc/server-settings/panel-domain-publisher.key) через скрытый ввод/доверенный канал. Не передавайте значение аргументомCLI, вGit, CI или отчёт. На этом узле прежний файл1panel-api.key не совпадал с актуальным ключом шлюза; он не перезаписывался, для publisher использован отдельный закрытый файл. API-клиент фиксирован наloopback, игнорирует environment proxies и запрещает redirects с подписанными headers.

```bash
# Только чтение/план — не изменяет сайт или сертификат
sudo python3 scripts/publish-panel-https.py --key-file PRIVATE_KEY_FILE

# Создать принадлежащий publisher сайт с недоступным upstream9 и запросить сертификат
sudo python3 scripts/publish-panel-https.py --stage prepare --apply \
  --expected-host ACTUAL_HOSTNAME --expected-ip ACTUAL_PUBLIC_IPV4 \
  --acme-account-id EXISTING_ACME_ID --key-file PRIVATE_KEY_FILE

# Подождать готовности сертификата; подтвердитьTLS+redirect API; только затем подключить панель
sudo python3 scripts/publish-panel-https.py --stage activate --apply \
  --expected-host ACTUAL_HOSTNAME --expected-ip ACTUAL_PUBLIC_IPV4 \
  --key-file PRIVATE_KEY_FILE
```

Проверяются hostname,DNSIPv4, уникальность и принадлежность сайта aliasasa-admin-panel-https, одинrootproxy, готовность/сроксертификата/autoRenew и фактическийHTTPSredirect вAPI. Чужой существующий сайт автоматически не перезаписывается. До активации нет проксирования к панели: initialupstream9, приошибкесертификата не публикуется loginHTTP. После prepare, еслиACMEневыдан, не повторять создание вслепую: проверить текущую запись/ACMElog, устранить причину и повторно запросить через1Panel. AAAA и внешнюю доступность проверять отдельно: IPv4проверка не валидируетIPv6.

## Откат без удаления данных

```bash
sudo python3 scripts/publish-panel-https.py --stage rollback --apply \
  --expected-host ACTUAL_HOSTNAME --key-file PRIVATE_KEY_FILE
```

Откат переводит **толькоэтот принадлежащийpublisher proxy** в503, оставляет HTTPS/ACME/сертификат для последующего восстановления. Не удаляет сайты, пользователей, сертификаты или тома. Аварийный вход остаётся черезLAN/SSHтуннель изdocs29. Повторныйactivate возвращает proxy послеTLSгейтов.

## Фактическая приёмка

- СертификатLet's Encrypt id8, SAN1panel.asa-lab.ru, ready, autoRenew=true; срокдо2027-01-31T09:41:54Z. Продление включено в1Panel, но будущее фактическое продление ещё не наблюдалось.
- СD2 через публичное имя иIP безSSHтуннеля: обычная валидацияCA/hostname PASS, TLS1.3, защищённыйentranceHTTP200, HTML/JS и форма1Panel отображаютсявChrome безTLSпредупреждения.
- HTTP301→HTTPS; HSTSmax-age31536000 получен на фактическомответе. Неавторизованный settingsAPI code401. Прямой публичный19192 timeout в проверке сD2.
- Существующий авторизованный MCPdashboard code200 после публикации. Проверить независимую внешнюю сеть/мобильную сеть владелец может полнымадресом: D2 использует публичное имя, но это не отдельный внешнийpentest.
- 7focusedunittestsPASS, включая планбезмутаций, чужойсайт, TLSgate, истёкшийсертификат, порядокактивации, rollback и реальный тестзапретаHTTPredirect/environmentproxy дляподписанногоAPI.
- Авторизованный browserlogin/терминал/WebSocketдействия не выполнялись; форма содержит лицензионное согласие, которое принимает владелец. Не заявляется полная боеготовность/MFAприёмка.
